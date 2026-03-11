"""Agent-facing memory tools.

These functions use :class:`MemoryStore` to create, search, list, update,
and forget memory files.  Context (memory_store, agent_name, username) is
set via :func:`configure` before tool calls.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

from operator_ai.memory import MemoryStore
from operator_ai.tools.registry import tool

logger = logging.getLogger("operator.tools.memory")

_context_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_memory_context", default=None
)


def configure(context: dict[str, Any]) -> None:
    _context_var.set(context)


def _get_context() -> tuple[MemoryStore, str, str, bool]:
    """Return (memory_store, agent_name, username, allow_user_scope)."""
    ctx = _context_var.get()
    if ctx is None:
        raise RuntimeError("memory tools not configured")
    store = ctx.get("memory_store")
    if store is None:
        raise RuntimeError("memory_store not set")
    return (
        store,
        ctx.get("agent_name", ""),
        ctx.get("user_id", ""),
        ctx.get("allow_user_scope", False),
    )


def _resolve_scope(scope: str, agent_name: str, username: str) -> str:
    """Map a tool-facing scope label to a MemoryStore scope string."""
    if scope == "agent":
        return f"agent:{agent_name}"
    if scope == "user":
        if not username:
            raise ValueError("username is required for user-scoped memory")
        return f"user:{username}"
    if scope == "global":
        return "global"
    raise ValueError(f"Invalid scope: {scope!r} (expected 'agent', 'user', or 'global')")


@tool(
    description="Create a rule memory (always injected into the prompt). Use for behavioral instructions that should shape every interaction.",
)
async def remember_rule(content: str, scope: str = "agent") -> str:
    """Create a rule memory.

    Args:
        content: The rule text.
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    if scope == "user" and not allow_user_scope:
        return "[error: user-scoped memory is only available in private conversations]"

    try:
        resolved = _resolve_scope(scope, agent_name, username)
    except ValueError as e:
        return f"[error: {e}]"

    path = memory_store.create_rule(resolved, content)
    logger.info("remember_rule: %s (scope=%s)", path, scope)
    return f"Rule saved: {path}"


@tool(
    description="Create a note memory (searched on demand). Use for durable knowledge that doesn't need to be injected every time.",
)
async def remember_note(content: str, scope: str = "agent", ttl: str = "") -> str:
    """Create a note memory.

    Args:
        content: The note text.
        scope: One of "agent", "user", or "global".
        ttl: Optional time-to-live (e.g. "3d", "2w", "1h", "30m"). Empty for no expiry.
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    if scope == "user" and not allow_user_scope:
        return "[error: user-scoped memory is only available in private conversations]"

    try:
        resolved = _resolve_scope(scope, agent_name, username)
    except ValueError as e:
        return f"[error: {e}]"

    try:
        path = memory_store.create_note(resolved, content, ttl=ttl or None)
    except ValueError as e:
        return f"[error: {e}]"

    ttl_msg = f" (expires in {ttl})" if ttl else ""
    logger.info("remember_note: %s (scope=%s%s)", path, scope, ttl_msg)
    return f"Note saved: {path}{ttl_msg}"


@tool(
    description="Search notes by filename and content.",
)
async def search_notes(query: str, scope: str = "agent") -> str:
    """Search notes.

    Args:
        query: Search query string.
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, _allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(scope, agent_name, username)
    except ValueError as e:
        return f"[error: {e}]"

    results = memory_store.search_notes(resolved, query)
    if not results:
        return "No matching notes found."

    lines: list[str] = []
    for mf in results:
        expires = f" [expires {mf.expires_at}]" if mf.expires_at else ""
        preview = mf.content[:120].replace("\n", " ")
        lines.append(f"[{mf.relative_path}]{expires} {preview}")
    return "\n".join(lines)


@tool(
    description="List all rule memories in a scope.",
)
async def list_rules(scope: str = "agent") -> str:
    """List rules.

    Args:
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, _allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(scope, agent_name, username)
    except ValueError as e:
        return f"[error: {e}]"

    results = memory_store.list_rules(resolved)
    if not results:
        return "No rules found."

    lines: list[str] = []
    for mf in results:
        preview = mf.content[:120].replace("\n", " ")
        lines.append(f"[{mf.relative_path}] {preview}")
    return "\n".join(lines)


@tool(
    description="List all note memories in a scope.",
)
async def list_notes(scope: str = "agent") -> str:
    """List notes.

    Args:
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, _allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(scope, agent_name, username)
    except ValueError as e:
        return f"[error: {e}]"

    results = memory_store.list_notes(resolved)
    if not results:
        return "No notes found."

    lines: list[str] = []
    for mf in results:
        expires = f" [expires {mf.expires_at}]" if mf.expires_at else ""
        preview = mf.content[:120].replace("\n", " ")
        lines.append(f"[{mf.relative_path}]{expires} {preview}")
    return "\n".join(lines)


@tool(
    description="Update an existing memory file's content.",
)
async def update_memory(path: str, content: str) -> str:
    """Update a memory file.

    Args:
        path: The relative path of the memory file.
        content: The new content.
    """
    memory_store, _agent_name, _username, _allow_user_scope = _get_context()

    if memory_store.update(path, content):
        logger.info("update_memory: %s", path)
        return f"Updated: {path}"
    return f"Not found: {path}"


@tool(
    description="Move a memory file to trash (soft delete).",
)
async def forget_memory(path: str) -> str:
    """Forget a memory file.

    Args:
        path: The relative path of the memory file.
    """
    memory_store, _agent_name, _username, _allow_user_scope = _get_context()

    if memory_store.forget(path):
        logger.info("forget_memory: %s", path)
        return f"Moved to trash: {path}"
    return f"Not found: {path}"
