from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from operator_ai.config import Config
from operator_ai.memory import MemoryStore
from operator_ai.prompts import assemble_system_prompt, load_prompt
from operator_ai.transport.base import MessageContext


@dataclass(frozen=True)
class ChatEnvelope:
    context: MessageContext
    transport_prompt: str = ""
    is_private: bool = False


@dataclass(frozen=True)
class JobEnvelope:
    name: str
    description: str
    schedule: str
    path: Path
    prerun_output: str = ""
    transport_prompt: str = ""


RunEnvelope: TypeAlias = ChatEnvelope | JobEnvelope


def build_agent_system_prompt(
    config: Config,
    agent_name: str,
    *,
    memory_store: MemoryStore | None = None,
    username: str = "",
    skill_filter=None,
    allowed_agents: set[str] | None = None,
    run_envelope: RunEnvelope | None = None,
) -> str:
    return assemble_system_prompt(
        config=config,
        agent_name=agent_name,
        memory_store=memory_store,
        username=username,
        is_private=_is_private(run_envelope),
        transport_extra=_render_run_envelope(config, agent_name, run_envelope),
        skill_filter=skill_filter,
        allowed_agents=allowed_agents,
    )


def _is_private(run_envelope: RunEnvelope | None) -> bool:
    return isinstance(run_envelope, ChatEnvelope) and run_envelope.is_private


def _render_run_envelope(
    config: Config,
    agent_name: str,
    run_envelope: RunEnvelope | None,
) -> str:
    if run_envelope is None:
        return ""
    if isinstance(run_envelope, ChatEnvelope):
        return _render_chat_envelope(config, agent_name, run_envelope)
    return _render_job_envelope(config, agent_name, run_envelope)


def _render_chat_envelope(
    config: Config,
    agent_name: str,
    envelope: ChatEnvelope,
) -> str:
    sections: list[str] = []
    if envelope.transport_prompt.strip():
        sections.append(envelope.transport_prompt.strip())

    ctx = envelope.context
    lines = [
        "# Context",
        "",
        f"- Platform: {ctx.platform}",
    ]
    lines.append(f"- Agent (You): {agent_name}")
    if ctx.chat_type:
        lines.append(f"- Chat type: {ctx.chat_type}")
    lines.append(f"- Channel: {ctx.channel_name} (`{ctx.channel_id}`)")
    if ctx.username:
        lines.append(f"- Username: {ctx.username}")
        lines.append(f"- Name: {ctx.user_name}")
    else:
        lines.append(f"- User: {ctx.user_name} (`{ctx.user_id}`)")
    if ctx.roles:
        lines.append(f"- Roles: {', '.join(ctx.roles)}")
    if ctx.timezone:
        lines.append(f"- Timezone: {ctx.timezone}")
    elif ctx.username:
        lines.append("- Timezone: *not set — please ask the user for their timezone*")
    lines.append(f"- Workspace: `{config.agent_workspace(agent_name)}`")
    lines.append(f"- Operator home: `{config.base_dir}` (also `$OPERATOR_HOME`)")
    sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _render_job_envelope(
    config: Config,
    agent_name: str,
    envelope: JobEnvelope,
) -> str:
    workspace = config.agent_workspace(agent_name)
    job_details = (
        f"- Name: {envelope.name}\n"
        f"- Schedule: `{envelope.schedule}`\n"
        f"- Description: {envelope.description}\n"
        f"- Job file: `{envelope.path}`\n"
        f"- Workspace: `{workspace}`\n"
        f"- Operator home: `{config.base_dir}` (also `$OPERATOR_HOME`)"
    )

    sections: list[str] = [load_prompt("job.md").replace("{job_details}", job_details)]
    if envelope.transport_prompt.strip():
        sections.append(envelope.transport_prompt.strip())
    if envelope.prerun_output.strip():
        sections.append(f"<prerun_output>\n{envelope.prerun_output}\n</prerun_output>")
    return "\n\n".join(sections)
