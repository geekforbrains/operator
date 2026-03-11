"""Tests for runtime components: resolve_allowed_agents, ConversationRuntime, RuntimeManager."""

from __future__ import annotations

import asyncio
import contextlib

from operator_ai.config import RoleConfig
from operator_ai.main import (
    AgentCancelledError,
    ConversationRuntime,
    RuntimeManager,
    resolve_allowed_agents,
)

# ── resolve_allowed_agents ────────────────────────────────────────


def test_admin_returns_none() -> None:
    """Admin role bypasses all restrictions — returns None (all access)."""
    result = resolve_allowed_agents(["admin"], {})
    assert result is None


def test_admin_with_other_roles_returns_none() -> None:
    """Admin combined with other roles still returns None."""
    roles_cfg = {"viewer": RoleConfig(agents=["hermy"])}
    result = resolve_allowed_agents(["admin", "viewer"], roles_cfg)
    assert result is None


def test_single_role_returns_agents() -> None:
    """A non-admin role returns its configured agents."""
    roles_cfg = {"ops": RoleConfig(agents=["hermy", "cora"])}
    result = resolve_allowed_agents(["ops"], roles_cfg)
    assert result == {"hermy", "cora"}


def test_multiple_roles_union() -> None:
    """Multiple roles produce a union of their agent sets."""
    roles_cfg = {
        "ops": RoleConfig(agents=["hermy"]),
        "dev": RoleConfig(agents=["cora", "pearl"]),
    }
    result = resolve_allowed_agents(["ops", "dev"], roles_cfg)
    assert result == {"hermy", "cora", "pearl"}


def test_unknown_role_ignored() -> None:
    """Roles not in config are silently ignored."""
    roles_cfg = {"ops": RoleConfig(agents=["hermy"])}
    result = resolve_allowed_agents(["ops", "nonexistent"], roles_cfg)
    assert result == {"hermy"}


def test_no_roles_returns_empty() -> None:
    """No roles at all → empty set (no access)."""
    result = resolve_allowed_agents([], {})
    assert result == set()


def test_role_with_no_agents() -> None:
    """A role with an empty agents list returns empty set."""
    roles_cfg = {"empty": RoleConfig(agents=[])}
    result = resolve_allowed_agents(["empty"], roles_cfg)
    assert result == set()


# ── ConversationRuntime ────────────────────────────────────────────


def test_claim_and_release() -> None:
    """Basic claim/release cycle."""
    rt = ConversationRuntime()
    assert not rt.busy

    assert rt.try_claim() is True
    assert rt.busy

    # Can't claim twice
    assert rt.try_claim() is False

    rt.release()
    assert not rt.busy

    # Can claim again after release
    assert rt.try_claim() is True


def test_cancel_sets_event() -> None:
    """cancel() sets the cancelled event."""
    rt = ConversationRuntime()
    assert not rt.cancelled.is_set()

    rt.cancel()
    assert rt.cancelled.is_set()


def test_check_cancelled_raises() -> None:
    """check_cancelled raises AgentCancelledError when cancelled."""
    rt = ConversationRuntime()
    rt.cancel()

    try:
        rt.check_cancelled()
        raised = False
    except AgentCancelledError:
        raised = True

    assert raised

    # After raising, the event is cleared
    assert not rt.cancelled.is_set()

    # Subsequent call doesn't raise
    rt.check_cancelled()  # should not raise


def test_release_clears_cancelled() -> None:
    """release() clears the cancelled event."""
    rt = ConversationRuntime()
    rt.try_claim()
    rt.cancel()
    assert rt.cancelled.is_set()

    rt.release()
    assert not rt.cancelled.is_set()


def test_attach_task_and_cancel() -> None:
    """cancel() cancels the attached task."""

    async def _run() -> None:
        rt = ConversationRuntime()
        rt.try_claim()

        async def _long_running() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(_long_running())
        rt.attach_task(task)
        rt.cancel()

        assert task.cancelled() or rt.cancelled.is_set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())


# ── RuntimeManager ────────────────────────────────────────────────


def test_get_or_create_new() -> None:
    """First call creates a new runtime."""
    mgr = RuntimeManager()
    rt = mgr.get_or_create("conv-1")
    assert isinstance(rt, ConversationRuntime)


def test_get_or_create_returns_same() -> None:
    """Subsequent calls for the same ID return the same runtime."""
    mgr = RuntimeManager()
    rt1 = mgr.get_or_create("conv-1")
    rt2 = mgr.get_or_create("conv-1")
    assert rt1 is rt2


def test_different_ids_different_runtimes() -> None:
    """Different conversation IDs get different runtimes."""
    mgr = RuntimeManager()
    rt1 = mgr.get_or_create("conv-1")
    rt2 = mgr.get_or_create("conv-2")
    assert rt1 is not rt2


def test_eviction_at_max() -> None:
    """When MAX_RUNTIMES is exceeded, oldest idle runtimes are evicted."""
    mgr = RuntimeManager()
    mgr._MAX_RUNTIMES = 3

    mgr.get_or_create("a")
    mgr.get_or_create("b")
    mgr.get_or_create("c")

    # Adding a 4th should evict the oldest (a)
    mgr.get_or_create("d")

    assert "a" not in mgr._runtimes
    assert "b" in mgr._runtimes
    assert "c" in mgr._runtimes
    assert "d" in mgr._runtimes


def test_eviction_skips_busy() -> None:
    """Eviction prefers idle runtimes over busy ones."""
    mgr = RuntimeManager()
    mgr._MAX_RUNTIMES = 3

    rt_a = mgr.get_or_create("a")
    rt_a.try_claim()  # mark as busy

    mgr.get_or_create("b")
    mgr.get_or_create("c")

    # Adding a 4th: 'a' is busy, so 'b' (oldest idle) gets evicted
    mgr.get_or_create("d")

    assert "a" in mgr._runtimes  # busy, kept
    assert "b" not in mgr._runtimes  # idle, evicted
    assert "c" in mgr._runtimes
    assert "d" in mgr._runtimes


def test_eviction_all_existing_busy_evicts_new_idle() -> None:
    """When all existing runtimes are busy, the newly added idle one is evicted
    (since _evict_one prefers idle runtimes). The returned runtime is still
    usable but no longer tracked in the dict."""
    mgr = RuntimeManager()
    mgr._MAX_RUNTIMES = 2

    rt_a = mgr.get_or_create("a")
    rt_a.try_claim()
    rt_b = mgr.get_or_create("b")
    rt_b.try_claim()

    # "c" is added but immediately evicted as idle (a and b are busy)
    rt_c = mgr.get_or_create("c")
    assert rt_c is not None  # still returned
    assert "a" in mgr._runtimes
    assert "b" in mgr._runtimes
    # "c" was evicted because it was the only idle runtime
    assert "c" not in mgr._runtimes


def test_get_or_create_moves_to_end() -> None:
    """Accessing an existing runtime moves it to the end (most recent)."""
    mgr = RuntimeManager()
    mgr._MAX_RUNTIMES = 3

    mgr.get_or_create("a")
    mgr.get_or_create("b")

    # Touch 'a' to move it to end
    mgr.get_or_create("a")

    mgr.get_or_create("c")

    # Now adding a 4th should evict 'b' (oldest), not 'a' (recently accessed)
    mgr.get_or_create("d")

    assert "a" in mgr._runtimes
    assert "b" not in mgr._runtimes
    assert "c" in mgr._runtimes
    assert "d" in mgr._runtimes
