from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from operator_ai.transport.base import Transport

logger = logging.getLogger("operator.status")

IDLE_MESSAGES = [
    "Pressing buttons...",
    "Generalizing knowledge...",
    "Consulting the oracle...",
    "Rearranging neurons...",
    "Connecting the dots...",
    "Warming up the hamsters...",
    "Pondering existence...",
    "Shuffling bits...",
    "Reading the fine print...",
    "Calibrating intuition...",
    "Asking nicely...",
    "Brewing thoughts...",
    "Summoning inspiration...",
    "Crunching context...",
    "Feeding the model...",
]


# Static tool labels — tool name -> display text (no args needed)
_STATIC_LABELS: dict[str, str] = {
    "list_files": "Listing files...",
    "run_shell": "Running command...",
    "send_message": "Sending message...",
    "spawn_agent": "Spawning sub-agent...",
    "save_rule": "Saving rule...",
    "save_note": "Saving note...",
    "search_notes": "Searching notes...",
    "forget_rule": "Forgetting rule...",
    "forget_note": "Forgetting note...",
    "list_rules": "Listing rules...",
    "list_notes": "Listing notes...",
    "create_job": "Creating job...",
    "update_job": "Updating job...",
    "delete_job": "Deleting job...",
    "enable_job": "Enabling job...",
    "disable_job": "Disabling job...",
    "list_jobs": "Listing jobs...",
    "get_state": "Reading state...",
    "set_state": "Saving state...",
    "append_state": "Appending state...",
    "pop_state": "Popping state...",
    "delete_state": "Deleting state...",
    "list_state": "Listing state...",
}


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else "..."


def _truncate_url(url: str, max_len: int = 50) -> str:
    return url[:max_len] + "..." if len(url) > max_len else url


# Dynamic tool labels — tool name -> formatter(args) -> display text
TOOL_LABELS: dict[str, Callable[[dict], str]] = {
    "read_file": lambda a: f"Reading `{_basename(a.get('path', ''))}`",
    "write_file": lambda a: f"Writing `{_basename(a.get('path', ''))}`",
    "web_fetch": lambda a: f"Fetching {_truncate_url(a.get('url', ''))}",
}


def _humanize(name: str) -> str:
    """Convert function_name to 'Function name...'."""
    words = re.sub(r"_", " ", name).strip()
    return (words[0].upper() + words[1:] + "...") if words else "Working..."


class StatusIndicator:
    """Transient status message shown while the agent is processing."""

    def __init__(
        self,
        transport: Transport,
        channel_id: str,
        thread_id: str | None = None,
        tool_labels: dict[str, Callable[[dict[str, Any]], str]] | None = None,
    ):
        self._transport = transport
        self._channel_id = channel_id
        self._thread_id = thread_id
        self._message_id: str | None = None
        self._ticker_task: asyncio.Task | None = None
        self._start_time: float = 0.0
        self._tool_label: str | None = None
        self._tool_labels = dict(tool_labels or {})

        # Shuffle idle messages for this run
        self._idle_messages = list(IDLE_MESSAGES)
        random.shuffle(self._idle_messages)
        self._idle_index = 0

    async def start(self) -> None:
        self._start_time = time.monotonic()
        text = self._format(self._next_idle())
        try:
            self._message_id = await self._transport.send(
                self._channel_id, text, thread_id=self._thread_id
            )
        except Exception:
            logger.debug("Failed to post status message", exc_info=True)
            return
        self._ticker_task = asyncio.create_task(self._tick_loop())

    def set_tool(self, name: str, args: dict[str, Any]) -> None:
        formatter = self._tool_labels.get(name) or TOOL_LABELS.get(name)
        if formatter:
            self._tool_label = formatter(args)
        elif name in _STATIC_LABELS:
            self._tool_label = _STATIC_LABELS[name]
        else:
            self._tool_label = _humanize(name)

    def clear_tool(self) -> None:
        self._tool_label = None

    async def stop(self) -> None:
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker_task
            self._ticker_task = None
        if self._message_id is not None:
            try:
                await self._transport.delete(self._channel_id, self._message_id)
            except Exception:
                logger.debug("Failed to delete status message", exc_info=True)
            self._message_id = None

    def _next_idle(self) -> str:
        msg = self._idle_messages[self._idle_index % len(self._idle_messages)]
        self._idle_index += 1
        return msg

    def _format(self, action: str) -> str:
        elapsed = int(time.monotonic() - self._start_time)
        return f"_({elapsed}s) {action}_"

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                action = self._tool_label or self._next_idle()
                text = self._format(action)
                try:
                    await self._transport.update(self._channel_id, self._message_id, text)
                except Exception:
                    logger.debug("Failed to update status message", exc_info=True)
        except asyncio.CancelledError:
            return
