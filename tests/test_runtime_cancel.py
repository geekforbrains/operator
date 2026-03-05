from __future__ import annotations

import asyncio

import pytest

from operator_ai.main import AgentCancelledError, ConversationRuntime


def test_cancel_cancels_attached_task() -> None:
    async def _run() -> None:
        runtime = ConversationRuntime()
        started = asyncio.Event()

        async def worker() -> None:
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(worker())
        runtime.attach_task(task)
        assert runtime.try_claim() is True
        await started.wait()

        runtime.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(AgentCancelledError):
            runtime.check_cancelled()
        runtime.release()

    asyncio.run(_run())


def test_cancel_without_attached_task_sets_flag_only() -> None:
    runtime = ConversationRuntime()
    runtime.cancel()
    with pytest.raises(AgentCancelledError):
        runtime.check_cancelled()


def test_release_clears_stale_cancel_flag() -> None:
    runtime = ConversationRuntime()
    assert runtime.try_claim() is True
    runtime.cancel()
    runtime.release()
    runtime.check_cancelled()  # should not raise
