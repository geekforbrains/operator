from __future__ import annotations

import asyncio
import contextvars
from typing import Any

from operator_ai.log_context import get_run_context, new_run_id, set_run_context
from operator_ai.prompts import assemble_system_prompt, load_prompt
from operator_ai.tools.registry import tool

MAX_SUBAGENT_DEPTH = 3

_context_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_agent_context", default=None
)


def configure(context: dict[str, Any]) -> None:
    _context_var.set(context)


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
    ctx["context_ratio"] = config.agent_context_ratio(agent_name)
    ctx["max_output_tokens"] = config.agent_max_output_tokens(agent_name)
    ctx["sandboxed"] = config.agent_sandboxed(agent_name)
    ctx["tool_filter"] = config.agent_tool_filter(agent_name)
    ctx["agent_name"] = agent_name
    return ctx


@tool(
    description="Spawn a sub-agent to handle a focused sub-task. The sub-agent gets its own conversation and runs to completion. Returns the sub-agent's final response.",
)
async def spawn_agent(task: str, context: str = "", agent: str = "") -> str:
    """Spawn a sub-agent for a focused sub-task.

    Args:
        task: Clear description of what the sub-agent should accomplish.
        context: Optional additional context or data for the sub-agent.
        agent: Optional agent name to spawn. Uses a different agent's config (prompt, models, workspace). If omitted, inherits the calling agent's context.
    """
    current_context = _context_var.get()
    if current_context is None:
        return "[error: subagent context not configured]"

    depth = current_context.get("depth", 0)
    if depth >= MAX_SUBAGENT_DEPTH:
        return f"[error: max subagent depth ({MAX_SUBAGENT_DEPTH}) reached]"

    try:
        resolved = _resolve_agent_context(agent or None, current_context)
    except ValueError as e:
        return f"[error: {e}]"

    # Build system prompt — use the target agent's prompt if spawning a different agent
    if agent and resolved.get("config"):
        system_prompt = assemble_system_prompt(
            config=resolved["config"],
            agent_name=agent,
        )
    else:
        system_prompt = load_prompt("subagent.md")

    if context:
        system_prompt += f"\n\nAdditional context:\n{context}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    # Lazy import to avoid circular dependency (agent -> subagent -> agent)
    from operator_ai.agent import run_agent

    parent_ctx = get_run_context()
    target_agent = agent or (parent_ctx.agent if parent_ctx else "sub")

    async def _child() -> str:
        set_run_context(
            agent=target_agent,
            run_id=parent_ctx.run_id if parent_ctx else new_run_id(),
            depth=depth + 1,
        )
        return await run_agent(
            messages=messages,
            models=resolved["models"],
            max_iterations=min(resolved.get("max_iterations", 10), 10),
            workspace=resolved.get("workspace", "."),
            depth=depth + 1,
            context_ratio=resolved.get("context_ratio", 0.0),
            max_output_tokens=resolved.get("max_output_tokens"),
            extra_tools=resolved.get("extra_tools"),
            usage=resolved.get("usage"),
            tool_filter=resolved.get("tool_filter"),
            shared_dir=resolved.get("shared_dir"),
            sandboxed=resolved.get("sandboxed", True),
            config=resolved.get("config"),
        )

    # Run in a copied context so the child's configure() call doesn't
    # overwrite the parent's ContextVars (depth, workspace, etc.).
    return await asyncio.create_task(_child(), context=contextvars.copy_context())
