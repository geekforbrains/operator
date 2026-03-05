from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from operator_ai.tools.registry import tool
from operator_ai.tools.workspace import get_workspace

_context_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("_messaging_context")


def configure(context: dict[str, Any]) -> None:
    _context_var.set(context)


@tool(
    description="Post a message to a channel. Returns a platform message ID.",
)
async def send_message(channel: str, text: str, thread_id: str = "") -> str:
    """Post a message to a channel.

    Args:
        channel: Channel name or ID (format depends on platform).
        text: Message content (markdown supported).
        thread_id: Optional message ID to reply in a thread (ignored if platform has no threading).
    """
    ctx = _context_var.get({})
    transport = ctx.get("transport")
    if transport is None:
        return "[error: no transport configured for send_message]"

    channel_id = await transport.resolve_channel_id(channel)
    if channel_id is None:
        return f"[error: could not resolve channel '{channel}']"

    try:
        message_id = await transport.send(channel_id, text, thread_id=thread_id or None)
        return message_id
    except Exception as e:
        return f"[error: failed to send message: {e}]"


@tool(
    description="Upload a file from the workspace to a channel. Returns a platform message ID.",
)
async def send_file(channel: str, path: str, thread_id: str = "") -> str:
    """Upload a file to a channel.

    Args:
        channel: Channel name or ID (format depends on platform).
        path: File path inside the agent workspace.
        thread_id: Optional message ID to reply in a thread.
    """
    ctx = _context_var.get({})
    transport = ctx.get("transport")
    if transport is None:
        return "[error: no transport configured for send_file]"

    channel_id = await transport.resolve_channel_id(channel)
    if channel_id is None:
        return f"[error: could not resolve channel '{channel}']"

    workspace = get_workspace().resolve()
    file_path = (workspace / Path(path).expanduser()).resolve()
    try:
        file_path.relative_to(workspace)
    except ValueError:
        return f"[error: path escapes workspace: {path}]"

    if not file_path.exists():
        return f"[error: file not found: {path}]"

    max_upload = 50 * 1024 * 1024  # 50 MB
    try:
        size = file_path.stat().st_size
        if size > max_upload:
            return f"[error: file too large ({size} bytes, limit {max_upload})]"
        file_data = file_path.read_bytes()
        message_id = await transport.send_file(
            channel_id, file_data, file_path.name, thread_id=thread_id or None
        )
        return message_id
    except NotImplementedError:
        return "[error: this transport does not support file uploads]"
    except Exception as e:
        return f"[error: failed to send file: {e}]"
