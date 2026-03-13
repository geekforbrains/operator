from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from operator_ai.utils import truncate

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


# Tool name -> formatter that takes args dict and returns display label
def _label_read_file(a: dict) -> str:
    return f"Reading `{_basename(a.get('path', ''))}`"


def _label_write_file(a: dict) -> str:
    return f"Writing `{_basename(a.get('path', ''))}`"


def _label_web_fetch(a: dict) -> str:
    return f"Fetching {truncate(a.get('url', ''), 50)}"


def _label_static(text: str) -> Callable[[dict], str]:
    def _fmt(_a: dict) -> str:
        return text

    return _fmt


TOOL_LABELS: dict[str, Callable[[dict], str]] = {
    "read_file": _label_read_file,
    "write_file": _label_write_file,
    "list_files": _label_static("Listing files..."),
    "run_shell": _label_static("Running command..."),
    "web_fetch": _label_web_fetch,
    "send_message": _label_static("Sending message..."),
    "spawn_agent": _label_static("Spawning sub-agent..."),
    "save_rule": _label_static("Saving rule..."),
    "save_note": _label_static("Saving note..."),
    "search_notes": _label_static("Searching notes..."),
    "forget_rule": _label_static("Forgetting rule..."),
    "forget_note": _label_static("Forgetting note..."),
    "list_rules": _label_static("Listing rules..."),
    "list_notes": _label_static("Listing notes..."),
    "create_job": _label_static("Creating job..."),
    "update_job": _label_static("Updating job..."),
    "delete_job": _label_static("Deleting job..."),
    "enable_job": _label_static("Enabling job..."),
    "disable_job": _label_static("Disabling job..."),
    "list_jobs": _label_static("Listing jobs..."),
    "get_state": _label_static("Reading state..."),
    "set_state": _label_static("Saving state..."),
    "append_state": _label_static("Appending state..."),
    "pop_state": _label_static("Popping state..."),
    "delete_state": _label_static("Deleting state..."),
    "list_state": _label_static("Listing state..."),
}


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else "..."


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
