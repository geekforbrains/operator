from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from operator_ai.agent_runtime import configure_agent_tool_context, resolve_base_dir
from operator_ai.log_context import get_run_context, new_run_id, set_run_context
from operator_ai.message_timestamps import attach_message_created_at
from operator_ai.prompts import assemble_system_prompt, load_prompt
from operator_ai.tools.context import get_user_context
from operator_ai.tools.registry import tool

logger = logging.getLogger("operator.subagent")

MAX_SUBAGENT_DEPTH = 3

_context_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_agent_context", default=None
)


def configure(context: dict[str, Any]) -> None:
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


def _resolve_agent_context(agent_name: str | None, current: dict[str, Any]) -> dict[str, Any]:
    """Resolve context for spawn_agent, switching to a different agent if specified."""
    if not agent_name:
        return current

    config = current.get("config")
    if config is None:
        return current

    if agent_name not in config.agents:
        raise ValueError(f"unknown agent '{agent_name}'")

    ctx = dict(current)
    ctx["models"] = config.agent_models(agent_name)
    ctx["max_iterations"] = config.agent_max_iterations(agent_name)
    ctx["workspace"] = str(config.agent_workspace(agent_name))
    ctx["thinking"] = config.agent_thinking(agent_name)
    ctx["context_ratio"] = config.agent_context_ratio(agent_name)
    ctx["max_output_tokens"] = config.agent_max_output_tokens(agent_name)
    ctx["tool_filter"] = config.agent_tool_filter(agent_name)
    ctx["skill_filter"] = config.agent_skill_filter(agent_name)
    ctx["agent_name"] = agent_name
    return ctx


def _build_subagent_prompt(
    resolved: dict[str, Any],
    *,
    target_agent: str,
    context: str,
) -> str:
    config = resolved.get("config")
    sections = [load_prompt("subagent.md")]

    user_ctx = get_user_context()
    context_lines = ["# Context", ""]
    if target_agent:
        context_lines.append(f"- Agent (You): {target_agent}")
    username = str(resolved.get("username") or "")
    if user_ctx is not None:
        username = user_ctx.username or username
    if username:
        context_lines.append(f"- Username: {username}")
    if user_ctx is not None:
        if user_ctx.roles:
            context_lines.append(f"- Roles: {', '.join(user_ctx.roles)}")
        if user_ctx.timezone:
            context_lines.append(f"- Timezone: {user_ctx.timezone}")

    workspace = resolved.get("workspace")
    if workspace:
        context_lines.append(f"- Workspace: `{workspace}`")
    operator_home = resolve_base_dir(config=config, base_dir=resolved.get("base_dir"))
    context_lines.append(f"- Operator home: `{operator_home}` (also `$OPERATOR_HOME`)")
    sections.append("\n".join(context_lines))

    if context:
        sections.append(f"## Additional Context\n\n{context}")

    extra = "\n\n".join(sections)

    if config is None or not target_agent:
        return extra

    return assemble_system_prompt(
        config=config,
        agent_name=target_agent,
        memory_store=resolved.get("memory_store"),
        username=str(resolved.get("username") or ""),
        is_private=bool(resolved.get("allow_user_scope", False)),
        transport_extra=extra,
        skill_filter=config.agent_skill_filter(target_agent),
        allowed_agents=resolved.get("allowed_agents"),
    )


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

    depth = current_context.get("depth", 0)
    if depth >= MAX_SUBAGENT_DEPTH:
        return f"[error: max subagent depth ({MAX_SUBAGENT_DEPTH}) reached]"

    # Check user-level access to the target agent
    if agent:
        config = current_context.get("config")
        if config and not _user_can_access_agent(agent, config):
            return f"[error: you don't have access to agent '{agent}']"

    try:
        resolved = _resolve_agent_context(agent or None, current_context)
    except ValueError as e:
        return f"[error: {e}]"

    target_agent = str(
        resolved.get("agent_name") or agent or current_context.get("agent_name") or ""
    )
    system_prompt = _build_subagent_prompt(
        resolved,
        target_agent=target_agent,
        context=context,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        attach_message_created_at({"role": "user", "content": task}),
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
        base_dir = resolve_base_dir(config=resolved.get("config"), base_dir=resolved.get("base_dir"))
        configure_agent_tool_context(
            agent_name=run_agent_name,
            base_dir=base_dir,
            skill_filter=resolved.get("skill_filter"),
            memory_store=resolved.get("memory_store"),
            username=str(resolved.get("username") or ""),
            allow_user_scope=bool(resolved.get("allow_user_scope", False)),
        )
        return await run_agent(
            messages=messages,
            models=resolved["models"],
            max_iterations=min(resolved.get("max_iterations", 10), 10),
            workspace=resolved.get("workspace", "."),
            agent_name=run_agent_name,
            depth=depth + 1,
            context_ratio=resolved.get("context_ratio", 0.0),
            max_output_tokens=resolved.get("max_output_tokens"),
            thinking=resolved.get("thinking", "off"),
            extra_tools=resolved.get("extra_tools"),
            usage=resolved.get("usage"),
            tool_filter=resolved.get("tool_filter"),
            skill_filter=resolved.get("skill_filter"),
            shared_dir=resolved.get("shared_dir"),
            config=resolved.get("config"),
            memory_store=resolved.get("memory_store"),
            username=str(resolved.get("username") or ""),
            allow_user_scope=bool(resolved.get("allow_user_scope", False)),
            allowed_agents=resolved.get("allowed_agents"),
            base_dir=base_dir,
        )

    # Run in a copied context so the child's configure() call doesn't
    # overwrite the parent's ContextVars (depth, workspace, etc.).
    return await asyncio.create_task(_child(), context=contextvars.copy_context())
