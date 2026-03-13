from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from operator_ai.agent.runtime import configure_agent_tool_context, resolve_base_dir
from operator_ai.config import Config, ThinkingLevel
from operator_ai.log_context import get_run_context, new_run_id, set_run_context
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import attach_message_created_at
from operator_ai.run_prompt import RunEnvelope, build_agent_system_prompt
from operator_ai.tools.context import get_user_context
from operator_ai.tools.registry import ToolDef, tool

logger = logging.getLogger("operator.subagent")

MAX_SUBAGENT_DEPTH = 3


@dataclass
class RunConfig:
    models: list[str]
    max_iterations: int
    workspace: str
    agent_name: str = ""
    depth: int = 0
    context_ratio: float = 0.0
    max_output_tokens: int | None = None
    thinking: ThinkingLevel = "off"
    extra_tools: list[ToolDef] | None = None
    usage: dict[str, int] | None = None
    tool_filter: Callable[[str], bool] | None = None
    skill_filter: Callable[[str], bool] | None = None
    shared_dir: Path | None = None
    config: Config | None = None
    memory_store: MemoryStore | None = None
    username: str = ""
    allow_user_scope: bool = False
    allowed_agents: set[str] | None = None
    base_dir: Path | None = None
    run_envelope: RunEnvelope | None = None


SubagentContext = RunConfig

_context_var: contextvars.ContextVar[RunConfig | None] = contextvars.ContextVar(
    "_agent_context", default=None
)


def configure(context: RunConfig) -> None:
    _context_var.set(context)


def _user_can_access_agent(agent_name: str, config: Any) -> bool:
    """Check if the current user's roles grant access to the target agent."""
    user_ctx = get_user_context()
    if user_ctx is None:
        # No user context (e.g., job runs) — allow
        return True
    if "admin" in user_ctx.roles:
        return True
    for role_name in user_ctx.roles:
        role_cfg = config.roles.get(role_name)
        if role_cfg and agent_name in role_cfg.agents:
            return True
    return False


def _resolve_agent_context(agent_name: str | None, current: RunConfig) -> RunConfig:
    """Resolve context for spawn_agent, switching to a different agent if specified."""
    if not agent_name:
        return current

    config = current.config
    if config is None:
        return current

    if agent_name not in config.agents:
        raise ValueError(f"unknown agent '{agent_name}'")

    return replace(
        current,
        models=config.agent_models(agent_name),
        max_iterations=config.agent_max_iterations(agent_name),
        workspace=str(config.agent_workspace(agent_name)),
        thinking=config.agent_thinking(agent_name),
        context_ratio=config.agent_context_ratio(agent_name),
        max_output_tokens=config.agent_max_output_tokens(agent_name),
        tool_filter=config.agent_tool_filter(agent_name),
        skill_filter=config.agent_skill_filter(agent_name),
        agent_name=agent_name,
    )


def _build_user_message(task: str, context: str) -> dict[str, Any]:
    content = task
    if context.strip():
        content = f"{task}\n\n<additional_context>\n{context.strip()}\n</additional_context>"
    return attach_message_created_at({"role": "user", "content": content})


@tool(
    description="Spawn a sub-agent to handle a focused sub-task. Omitting agent starts a fresh run of the current agent; specifying agent switches to that agent's own prompt, memory, tools, and workspace. Returns the sub-agent's final response.",
)
async def spawn_agent(task: str, context: str = "", agent: str = "") -> str:
    """Spawn a sub-agent for a focused sub-task.

    Args:
        task: Clear description of what the sub-agent should accomplish.
        context: Optional additional context or data for the sub-agent.
        agent: Optional agent name to spawn. If omitted, starts a fresh run of the current agent. If provided, uses the target agent's own config, memory, skills, and workspace.
    """
    current_context = _context_var.get()
    if current_context is None:
        return "[error: subagent context not configured]"

    depth = current_context.depth
    if depth >= MAX_SUBAGENT_DEPTH:
        return f"[error: max subagent depth ({MAX_SUBAGENT_DEPTH}) reached]"

    # Check user-level access to the target agent
    if agent:
        config = current_context.config
        if config and not _user_can_access_agent(agent, config):
            return f"[error: you don't have access to agent '{agent}']"

    try:
        resolved = _resolve_agent_context(agent or None, current_context)
    except ValueError as e:
        return f"[error: {e}]"

    target_agent = resolved.agent_name or agent or current_context.agent_name
    if not target_agent:
        return "[error: target agent not configured]"
    if resolved.config is None:
        return "[error: config not available for subagent run]"

    system_prompt = build_agent_system_prompt(
        config=resolved.config,
        agent_name=target_agent,
        memory_store=resolved.memory_store,
        username=resolved.username,
        skill_filter=resolved.skill_filter,
        allowed_agents=resolved.allowed_agents,
        run_envelope=resolved.run_envelope,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        _build_user_message(task, context),
    ]

    # Lazy import to avoid circular dependency (agent -> subagent -> agent)
    from operator_ai.agent import run_agent

    parent_ctx = get_run_context()
    parent_agent = parent_ctx.agent if parent_ctx else "unknown"
    run_agent_name = target_agent or (parent_ctx.agent if parent_ctx else "sub")

    if agent:
        logger.info(
            "spawning agent '%s' from '%s' (depth %d)", run_agent_name, parent_agent, depth + 1
        )
    else:
        logger.info("spawning sub-agent from '%s' (depth %d)", parent_agent, depth + 1)

    async def _child() -> str:
        set_run_context(
            agent=run_agent_name,
            run_id=parent_ctx.run_id if parent_ctx else new_run_id(),
            depth=depth + 1,
        )
        base_dir = resolve_base_dir(config=resolved.config, base_dir=resolved.base_dir)
        configure_agent_tool_context(
            agent_name=run_agent_name,
            base_dir=base_dir,
            skill_filter=resolved.skill_filter,
            memory_store=resolved.memory_store,
            username=resolved.username,
            allow_user_scope=resolved.allow_user_scope,
        )
        child_rc = replace(
            resolved,
            workspace=resolved.workspace or ".",
            agent_name=run_agent_name,
            depth=depth + 1,
            base_dir=base_dir,
        )
        return await run_agent(
            messages=messages,
            rc=child_rc,
        )

    # Run in a copied context so the child's configure() call doesn't
    # overwrite the parent's ContextVars (depth, workspace, etc.).
    return await asyncio.create_task(_child(), context=contextvars.copy_context())
