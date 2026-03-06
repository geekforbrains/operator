from __future__ import annotations

import contextvars
from typing import Any

from operator_ai.tools.files import _resolve
from operator_ai.tools.registry import tool

_context_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("_messaging_context")


def configure(context: dict[str, Any]) -> None:
    _context_var.set(context)


@tool(
    description="Post a message to a channel. Returns a platform message ID.",
)
async def send_message(channel: str = "", text: str = "", thread_id: str = "") -> str:
    """Post a message to a channel.

    Args:
        channel: Channel name or ID. Defaults to the current conversation channel.
        text: Message content (markdown supported).
        thread_id: Message ID to reply in a thread. Defaults to the current thread.
    """
    ctx = _context_var.get({})
    transport = ctx.get("transport")
    if transport is None:
        return "[error: no transport configured for send_message]"

    channel_id = await _resolve_channel(ctx, transport, channel)
    if channel_id is None:
        target = f" '{channel}'" if channel else ""
        return f"[error: could not resolve channel{target}]"
    tid = thread_id or ctx.get("thread_id") or None

    try:
        message_id = await transport.send(channel_id, text, thread_id=tid)
        return message_id
    except Exception as e:
        return f"[error: failed to send message: {e}]"


@tool(
    description="Upload a file to a channel. Returns a platform message ID.",
)
async def send_file(path: str, channel: str = "", thread_id: str = "") -> str:
    """Upload a file to a channel.

    Args:
        path: File path (relative to workspace, or absolute when unsandboxed).
        channel: Channel name or ID. Defaults to the current conversation channel.
        thread_id: Message ID to reply in a thread. Defaults to the current thread.
    """
    ctx = _context_var.get({})
    transport = ctx.get("transport")
    if transport is None:
        return "[error: no transport configured for send_file]"

    channel_id = await _resolve_channel(ctx, transport, channel)
    if channel_id is None:
        target = f" '{channel}'" if channel else ""
        return f"[error: could not resolve channel{target}]"
    tid = thread_id or ctx.get("thread_id") or None

    try:
        file_path = _resolve(path)
    except ValueError as e:
        return f"[error: {e}]"

    if not file_path.exists():
        return f"[error: file not found: {path}]"

    max_upload = 50 * 1024 * 1024  # 50 MB
    try:
        size = file_path.stat().st_size
        if size > max_upload:
            return f"[error: file too large ({size} bytes, limit {max_upload})]"
        file_data = file_path.read_bytes()
        message_id = await transport.send_file(channel_id, file_data, file_path.name, thread_id=tid)
        return message_id
    except NotImplementedError:
        return "[error: this transport does not support file uploads]"
    except Exception as e:
        return f"[error: failed to send file: {e}]"


async def _resolve_channel(ctx: dict, transport: Any, channel: str) -> str | None:
    """Resolve channel to an ID, falling back to the current conversation's channel.

    Returns the channel ID on success, or ``None`` if resolution fails.
    """
    if channel:
        return await transport.resolve_channel_id(channel)
    return ctx.get("channel_id") or None
