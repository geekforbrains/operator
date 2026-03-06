from __future__ import annotations

import contextvars
import logging
from typing import Any

from operator_ai.tools.registry import tool

logger = logging.getLogger("operator.memory")

_context_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("_memory_context")


def configure(context: dict[str, Any]) -> None:
    _context_var.set(context)


def _allow_user_scope() -> bool:
    ctx = _context_var.get({})
    return bool(ctx.get("allow_user_scope", True))


def _scope_id(scope: str, ctx: dict[str, Any]) -> str:
    user_id = ctx.get("user_id", "")
    agent_name = ctx.get("agent_name", "default")
    return {"user": user_id, "agent": agent_name, "global": "global"}[scope]


@tool(
    description="Save a memory for future reference. Memories persist across conversations.",
)
async def save_memory(content: str, scope: str = "user", pinned: bool = False) -> str:
    """Save a fact or piece of information to long-term memory.

    Args:
        content: The fact or information to remember.
        scope: One of "user" (personal), "agent" (agent-specific), or "global" (shared).
        pinned: If true, the memory is always injected into the system prompt.
    """
    ctx = _context_var.get({})
    memory_store = ctx.get("memory_store")
    if memory_store is None:
        return "[error: memory system not configured]"

    if scope not in ("user", "agent", "global"):
        return f"[error: invalid scope '{scope}', must be user/agent/global]"
    if scope == "user" and not _allow_user_scope():
        return "[error: user-scoped memory is only allowed in private conversations]"

    scope_id = _scope_id(scope, ctx)
    if not scope_id:
        return "[error: no user_id available for user-scoped memory]"

    result = await memory_store.save(content, scope, scope_id, pinned=pinned)
    if result is None:
        logger.warning("save_memory: cap reached for %s/%s", scope, scope_id)
        return "[memory cap reached, memory not saved]"
    label = "pinned " if pinned else ""
    logger.info("save_memory: id=%d %sscope=%s/%s", result, label, scope, scope_id)
    return f"Memory saved (id={result}{', pinned' if pinned else ''})"


@tool(
    description="Search memories for information relevant to a query.",
)
async def search_memories(query: str, scope: str = "", top_k: int = 5) -> str:
    """Search long-term memory for relevant facts.

    Args:
        query: What to search for.
        scope: Filter to "user", "agent", or "global". Empty string searches all scopes.
        top_k: Maximum number of results.
    """
    ctx = _context_var.get({})
    memory_store = ctx.get("memory_store")
    if memory_store is None:
        return "[error: memory system not configured]"

    if scope:
        if scope not in ("user", "agent", "global"):
            return f"[error: invalid scope '{scope}', must be user/agent/global or empty]"
        if scope == "user" and not _allow_user_scope():
            return "[error: user-scoped memory is only allowed in private conversations]"
        scopes = [(scope, _scope_id(scope, ctx))]
    else:
        user_id = ctx.get("user_id", "")
        agent_name = ctx.get("agent_name", "default")
        scopes = [("agent", agent_name), ("global", "global")]
        if _allow_user_scope() and user_id:
            scopes.insert(0, ("user", user_id))

    results = await memory_store.search(query, scopes, top_k=top_k)
    logger.info("search_memories: query=%r → %d results", query[:60], len(results))
    if not results:
        return "No relevant memories found."

    lines = []
    for r in results:
        lines.append(
            f"[id={r['memory_id']}] [{r['scope']}] {r['content']} (relevance={r['relevance']})"
        )
    return "\n".join(lines)


@tool(
    description="Delete a memory by its ID.",
)
async def forget_memory(memory_id: int) -> str:
    """Remove a memory from long-term storage.

    Args:
        memory_id: The ID of the memory to delete.
    """
    ctx = _context_var.get({})
    memory_store = ctx.get("memory_store")
    if memory_store is None:
        return "[error: memory system not configured]"

    if memory_store.forget(memory_id):
        logger.info("forget_memory: deleted id=%d", memory_id)
        return f"Memory {memory_id} deleted."
    logger.info("forget_memory: id=%d not found", memory_id)
    return f"Memory {memory_id} not found."


@tool(
    description="List stored memories. Returns a deterministic listing, optionally filtered by scope.",
)
async def list_memories(scope: str = "", limit: int = 50, offset: int = 0) -> str:
    """List memories from long-term storage.

    Args:
        scope: Filter to "user", "agent", or "global". Empty string lists all scopes.
        limit: Maximum number of memories to return.
        offset: Number of memories to skip (for pagination).
    """
    ctx = _context_var.get({})
    memory_store = ctx.get("memory_store")
    if memory_store is None:
        return "[error: memory system not configured]"

    if scope:
        if scope not in ("user", "agent", "global"):
            return f"[error: invalid scope '{scope}', must be user/agent/global or empty]"
        if scope == "user" and not _allow_user_scope():
            return "[error: user-scoped memory is only allowed in private conversations]"
        results = memory_store.list_memories(scope, _scope_id(scope, ctx), limit, offset)
    else:
        if _allow_user_scope():
            results = memory_store.list_memories(limit=limit, offset=offset)
        else:
            # Public conversations should not expose user-scoped memories.
            agent_name = ctx.get("agent_name", "default")
            pool_size = max(limit + offset, limit)
            results = memory_store.list_memories("agent", agent_name, pool_size, 0)
            results.extend(memory_store.list_memories("global", "global", pool_size, 0))
            results.sort(key=lambda m: int(m["id"]))
            results = results[offset : offset + limit]

    if not results:
        return "No memories found."

    lines = []
    for m in results:
        pin = " [PINNED]" if m.get("pinned") else ""
        lines.append(f"[id={m['id']}] [{m['scope']}] {m['content']}{pin}")
    return "\n".join(lines)
