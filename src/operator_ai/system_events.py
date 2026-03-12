"""Ephemeral per-conversation system event buffer.

System events are lightweight notifications (reactions, pins, membership
changes, etc.) that are too noisy to trigger a full agent run but useful
as context when the agent next responds to a real message.

Events are stored in memory only — no persistence.  Each conversation is
capped at MAX_EVENTS entries (FIFO, oldest dropped).  Consecutive
duplicate texts are suppressed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("operator.system_events")

MAX_EVENTS = 20


@dataclass
class SystemEvent:
    text: str
    ts: float


class SystemEventBuffer:
    """In-memory per-conversation event buffer."""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._max_events = max_events
        self._queues: dict[str, list[SystemEvent]] = {}
        self._last_text: dict[str, str] = {}

    def enqueue(self, conversation_id: str, text: str) -> bool:
        """Append an event.  Returns False if empty or consecutive duplicate."""
        text = text.strip()
        if not text:
            return False
        if self._last_text.get(conversation_id) == text:
            logger.debug("Suppressed duplicate system event for %s", conversation_id)
            return False
        self._last_text[conversation_id] = text
        queue = self._queues.setdefault(conversation_id, [])
        queue.append(SystemEvent(text=text, ts=time.time()))
        if len(queue) > self._max_events:
            dropped = len(queue) - self._max_events
            del queue[:dropped]
            logger.debug("Dropped %d old event(s) for %s (at cap)", dropped, conversation_id)
        logger.info("System event queued for %s: %s", conversation_id, text)
        return True

    def drain(self, conversation_id: str) -> list[SystemEvent]:
        """Return and clear all events for a conversation."""
        events = self._queues.pop(conversation_id, [])
        self._last_text.pop(conversation_id, None)
        if events:
            logger.info("Drained %d system event(s) for %s", len(events), conversation_id)
        return events
