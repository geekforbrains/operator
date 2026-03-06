from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from operator_ai.config import SKILLS_DIR, Config
from operator_ai.job_specs import JOBS_DIR as _JOBS_DIR
from operator_ai.job_specs import find_job_spec, scan_job_specs
from operator_ai.skills import scan_skills
from operator_ai.store import Store

if TYPE_CHECKING:
    from operator_ai.main import ConversationRuntime
    from operator_ai.memory import MemoryStore
    from operator_ai.transport.base import Transport

logger = logging.getLogger("operator.commands")


@dataclass
class CommandContext:
    args: list[str]
    agent_name: str
    store: Store
    config: Config
    memory_store: MemoryStore | None
    runtime: ConversationRuntime
    transport: Transport


CommandHandler = Callable[[CommandContext], Awaitable[str]]


@dataclass
class CommandInfo:
    handler: CommandHandler
    description: str


COMMANDS: dict[str, CommandInfo] = {}


def command(name: str, description: str):
    """Decorator to register a chat command."""

    def decorator(func: CommandHandler) -> CommandHandler:
        COMMANDS[name] = CommandInfo(handler=func, description=description)
        return func

    return decorator


async def dispatch_command(cmd_name: str, ctx: CommandContext) -> str:
    info = COMMANDS.get(cmd_name)
    if info is None:
        safe = cmd_name.replace("`", "").replace("<", "").replace(">", "")
        return f"Unknown command: `!{safe}`. Type `!help` for a list of commands."
    try:
        return await info.handler(ctx)
    except Exception:
        logger.exception("Command '!%s' failed", cmd_name)
        return f"[error: command `!{cmd_name}` failed]"


# ── Commands ─────────────────────────────────────────────────


@command("help", "List available commands")
async def cmd_help(ctx: CommandContext) -> str:
    lines = ["*Available commands:*\n"]
    for name, info in COMMANDS.items():
        lines.append(f"`!{name}` — {info.description}")
    return "\n".join(lines)


@command("stop", "Cancel the active request")
async def cmd_stop(ctx: CommandContext) -> str:
    if ctx.runtime.busy:
        ctx.runtime.cancel()
        logger.info("[%s] !stop — cancelling active request", ctx.agent_name)
        return "Cancelling…"
    return "No active request to stop."


@command("restart", "Restart the background service")
async def cmd_restart(ctx: CommandContext) -> str:
    return "Service restart is disabled in chat. Use `operator service restart` on the host."


@command("agents", "List configured agents")
async def cmd_agents(ctx: CommandContext) -> str:
    if not ctx.config.agents:
        return "No agents configured."

    lines = ["*Agents:*\n"]
    for name, agent in ctx.config.agents.items():
        models = ", ".join(agent.models) if agent.models else ", ".join(ctx.config.defaults.models)
        transport_type = agent.transport.type if agent.transport else "none"
        lines.append(f"*{name}* — transport: `{transport_type}`, models: `{models}`")
    return "\n".join(lines)


@command("jobs", "List jobs or show details for a specific job")
async def cmd_jobs(ctx: CommandContext) -> str:
    if ctx.args:
        return await _job_subcommand(ctx)
    return _list_jobs(ctx)


def _list_jobs(ctx: CommandContext) -> str:
    jobs = scan_job_specs(_JOBS_DIR)
    if not jobs:
        return "No jobs found."

    lines = ["*Jobs:*\n"]
    for job in jobs:
        state = ctx.store.load_job_state(job.name)
        status = "enabled" if job.enabled else "disabled"
        last = state.last_run[:19] if state.last_run else "never"
        result = state.last_result or "-"
        lines.append(
            f"{'>' if job.enabled else 'x'} *{job.name}* "
            f"[{status}] `{job.schedule}`\n"
            f"  Last: {last} ({result}) | "
            f"Runs: {state.run_count} | Errors: {state.error_count} | "
            f"Gates: {state.gate_count} | Skips: {state.skip_count}"
        )
    return "\n".join(lines)


async def _job_subcommand(ctx: CommandContext) -> str:
    job_name = ctx.args[0]
    action = ctx.args[1].lower() if len(ctx.args) > 1 else None

    if action == "enable":
        return "Job enable/disable is disabled in chat. Use `operator job enable <name>`."
    elif action == "disable":
        return "Job enable/disable is disabled in chat. Use `operator job disable <name>`."

    # Show single job details
    job = find_job_spec(job_name, _JOBS_DIR)
    if not job:
        return f"Job `{job_name}` not found."

    state = ctx.store.load_job_state(job.name)
    status = "enabled" if job.enabled else "disabled"
    last = state.last_run[:19] if state.last_run else "never"
    result = state.last_result or "-"

    lines = [
        f"*{job.name}* [{status}]",
        f"Schedule: `{job.schedule}`",
        f"Description: {job.description or '-'}",
        "",
        f"Last run: {last}",
        f"Last result: {result}",
    ]
    if state.last_duration_seconds:
        lines.append(f"Duration: {state.last_duration_seconds}s")
    if state.last_error:
        lines.append(f"Last error: {state.last_error}")
    lines.append(
        f"Runs: {state.run_count} | Errors: {state.error_count} | "
        f"Gates: {state.gate_count} | Skips: {state.skip_count}"
    )
    return "\n".join(lines)


@command("skills", "List discovered skills")
async def cmd_skills(ctx: CommandContext) -> str:
    skills = scan_skills(SKILLS_DIR)
    if not skills:
        return "No skills found."

    lines = ["*Skills:*\n"]
    for s in skills:
        desc = s.description
        if len(desc) > 80:
            desc = desc[:77] + "..."
        env_note = ""
        if s.env_missing:
            env_note = f" (missing env: {', '.join(s.env_missing)})"
        elif s.env:
            env_note = " (env: ok)"
        lines.append(f"*{s.name}* — {desc}{env_note}")
    return "\n".join(lines)


@command("memories", "List pinned memories")
async def cmd_memories(ctx: CommandContext) -> str:
    if not ctx.memory_store:
        return "Memory system is not enabled."

    if ctx.args:
        return await _memories_subcommand(ctx)
    return _list_pinned_memories(ctx)


def _list_pinned_memories(ctx: CommandContext) -> str:
    scopes = [
        ("agent", ctx.agent_name),
        ("global", "global"),
    ]
    all_pinned: list[dict[str, Any]] = []
    for scope, scope_id in scopes:
        all_pinned.extend(ctx.memory_store.get_pinned_memories(scope, scope_id))  # type: ignore[union-attr]

    if not all_pinned:
        return "No pinned memories."

    lines = ["*Pinned memories:*\n"]
    for m in all_pinned:
        content = m["content"].replace("\n", " ")
        if len(content) > 120:
            content = content[:117] + "..."
        lines.append(f"`#{m['id']}` [{m['scope']}/{m['scope_id']}] {content}")
    return "\n".join(lines)


async def _memories_subcommand(ctx: CommandContext) -> str:
    action = ctx.args[0].lower()

    if action == "clear":
        return "Memory mutation is disabled in chat. Use CLI tooling for memory updates."

    if action == "delete":
        return "Memory mutation is disabled in chat. Use CLI tooling for memory updates."

    return f"Unknown memories subcommand: `{action}`. Use `clear` or `delete <id>`."
