"""Agent-facing deterministic memory tools.

These functions use :class:`MemoryStore` to save, search, list, and forget
memory items without exposing filesystem paths to the agent.
"""

from __future__ import annotations

import contextvars
import logging
from datetime import datetime
from typing import Any, Literal

from operator_ai.memory import MemoryFile, MemoryStore
from operator_ai.tools.registry import tool

logger = logging.getLogger("operator.tools.memory")

MemoryScope = Literal["agent", "user", "global"]

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
        ctx.get("username", ctx.get("user_id", "")),
        ctx.get("allow_user_scope", False),
    )


def _resolve_scope(
    scope: str,
    *,
    agent_name: str,
    username: str,
    allow_user_scope: bool,
) -> str:
    """Map a tool-facing scope label to a MemoryStore scope string."""
    if scope == "agent":
        return f"agent:{agent_name}"
    if scope == "user":
        if not allow_user_scope:
            raise ValueError("user-scoped memory is only available in private conversations")
        if not username:
            raise ValueError("username is required for user-scoped memory")
        return f"user:{username}"
    if scope == "global":
        return "global"
    raise ValueError(f"Invalid scope: {scope!r} (expected 'agent', 'user', or 'global')")


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat().replace("+00:00", "Z")


def _format_memory_line(mf: MemoryFile) -> str:
    expires = _format_timestamp(mf.expires_at)
    expires_text = f" [expires {expires}]" if expires else ""
    preview = mf.content[:120].replace("\n", " ")
    return f"[{mf.key}]{expires_text} {preview}"


@tool(
    description="Create or replace a rule memory by deterministic key. Use for short standing instructions that should shape every interaction.",
)
async def save_rule(key: str, content: str, scope: MemoryScope = "agent") -> str:
    """Save a rule memory.

    Args:
        key: Short stable identifier such as "response-style" or "tooling-preference".
        content: The rule text.
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
        memory_store.upsert_rule(resolved, key, content)
    except ValueError as e:
        return f"[error: {e}]"

    logger.info("save_rule: %s (scope=%s)", key, scope)
    return f"Saved rule '{key}' in {scope} scope."


@tool(
    description="Create or replace a note memory by deterministic key. Use for durable knowledge that should be searched on demand instead of injected every time.",
)
async def save_note(key: str, content: str, scope: MemoryScope = "agent", ttl: str = "") -> str:
    """Save a note memory.

    Args:
        key: Short stable identifier such as "release-date" or "staging-api-url".
        content: The note text.
        scope: One of "agent", "user", or "global".
        ttl: Optional time-to-live (e.g. "3d", "2w", "1h", "30m"). Empty for no expiry.
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
        memory_store.upsert_note(resolved, key, content, ttl=ttl or None)
    except ValueError as e:
        return f"[error: {e}]"

    ttl_msg = f" (expires in {ttl})" if ttl else ""
    logger.info("save_note: %s (scope=%s%s)", key, scope, ttl_msg)
    return f"Saved note '{key}' in {scope} scope{ttl_msg}."


@tool(
    description="Search note memories by key and content within a scope.",
)
async def search_notes(query: str, scope: MemoryScope = "agent") -> str:
    """Search notes.

    Args:
        query: Search query string.
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
    except ValueError as e:
        return f"[error: {e}]"

    results = memory_store.search_notes(resolved, query)
    if not results:
        return "No matching notes found."

    return "\n".join(_format_memory_line(mf) for mf in results)


@tool(
    description="List all active rule memories in a scope.",
)
async def list_rules(scope: MemoryScope = "agent") -> str:
    """List rules.

    Args:
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
    except ValueError as e:
        return f"[error: {e}]"

    results = memory_store.list_rules(resolved)
    if not results:
        return "No rules found."

    return "\n".join(_format_memory_line(mf) for mf in results)


@tool(
    description="List active note memories in a scope. Use limit and offset to paginate large collections.",
)
async def list_notes(scope: MemoryScope = "agent", limit: int = 50, offset: int = 0) -> str:
    """List notes.

    Args:
        scope: One of "agent", "user", or "global".
        limit: Maximum number of notes to return (default 50).
        offset: Number of notes to skip for pagination (default 0).
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
    except ValueError as e:
        return f"[error: {e}]"

    all_notes = memory_store.list_notes(resolved)
    total = len(all_notes)
    if total == 0:
        return "No notes found."

    page = all_notes[offset : offset + limit]
    if not page:
        return f"No notes at offset {offset} (total: {total})."

    lines = [_format_memory_line(mf) for mf in page]
    if total > offset + limit:
        lines.append(
            f"[... {total - offset - limit} more — use offset={offset + limit} to continue]"
        )
    return "\n".join(lines)


@tool(
    description="Read the full content of a note by its key.",
)
async def read_note(key: str, scope: MemoryScope = "agent") -> str:
    """Read a note.

    Args:
        key: The deterministic key of the note.
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
    except ValueError as e:
        return f"[error: {e}]"

    mf = memory_store.get_note(resolved, key)
    if mf is None:
        return f"Note '{key}' not found in {scope} scope."

    expires = _format_timestamp(mf.expires_at)
    header = f"[{mf.key}]"
    if expires:
        header += f" [expires {expires}]"
    return f"{header}\n{mf.content}"


@tool(
    description="Move a rule memory to trash by deterministic key.",
)
async def forget_rule(key: str, scope: MemoryScope = "agent") -> str:
    """Forget a rule memory.

    Args:
        key: Short stable identifier of the rule to forget.
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
    except ValueError as e:
        return f"[error: {e}]"

    if memory_store.forget_rule(resolved, key):
        logger.info("forget_rule: %s (scope=%s)", key, scope)
        return f"Moved rule '{key}' to trash in {scope} scope."
    return f"Rule not found: {key}"


@tool(
    description="Move a note memory to trash by deterministic key.",
)
async def forget_note(key: str, scope: MemoryScope = "agent") -> str:
    """Forget a note memory.

    Args:
        key: Short stable identifier of the note to forget.
        scope: One of "agent", "user", or "global".
    """
    memory_store, agent_name, username, allow_user_scope = _get_context()

    try:
        resolved = _resolve_scope(
            scope,
            agent_name=agent_name,
            username=username,
            allow_user_scope=allow_user_scope,
        )
    except ValueError as e:
        return f"[error: {e}]"

    if memory_store.forget_note(resolved, key):
        logger.info("forget_note: %s (scope=%s)", key, scope)
        return f"Moved note '{key}' to trash in {scope} scope."
    return f"Note not found: {key}"
