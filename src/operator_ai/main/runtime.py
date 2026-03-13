from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("operator")


class AgentCancelledError(Exception):
    pass


class ConversationBusyError(Exception):
    pass


class RuntimeCapacityError(Exception):
    pass


class ConversationRuntime:
    def __init__(self) -> None:
        self._active = False
        self.cancelled = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._active

    def try_claim(self) -> bool:
        """Atomically check and mark as active.

        Because asyncio is single-threaded and this method contains no
        ``await``, the check-and-set is atomic — no other task can
        interleave between reading and writing ``_active``.
        """
        logger.debug("try_claim runtime=%s active=%s", id(self), self._active)
        if self._active:
            return False
        self._active = True
        return True

    def release(self) -> None:
        logger.debug("release runtime=%s", id(self))
        self._active = False
        self._task = None
        # Clear stale stop state so the next request in this thread starts cleanly.
        self.cancelled.clear()

    def attach_task(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def cancel(self) -> None:
        self.cancelled.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            self.cancelled.clear()
            raise AgentCancelledError()


class RuntimeManager:
    _MAX_ACTIVE_RUNTIMES = 256

    def __init__(self) -> None:
        self._runtimes: dict[str, ConversationRuntime] = {}

    def get(self, conversation_id: str) -> ConversationRuntime | None:
        return self._runtimes.get(conversation_id)

    def claim(self, conversation_id: str) -> ConversationRuntime:
        runtime = self._runtimes.get(conversation_id)
        if runtime is not None:
            raise ConversationBusyError()
        if len(self._runtimes) >= self._MAX_ACTIVE_RUNTIMES:
            raise RuntimeCapacityError()
        runtime = ConversationRuntime()
        runtime.try_claim()
        self._runtimes[conversation_id] = runtime
        return runtime

    def release(self, conversation_id: str, runtime: ConversationRuntime) -> None:
        tracked = self._runtimes.get(conversation_id)
        runtime.release()
        if tracked is runtime:
            del self._runtimes[conversation_id]
        elif tracked is not None:
            logger.warning("Conversation %s runtime mismatch on release", conversation_id)
