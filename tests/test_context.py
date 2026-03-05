from __future__ import annotations

import asyncio
import contextvars

from operator_ai.tools.context import (
    ROLE_GATED_TOOLS,
    UserContext,
    get_skill_filter,
    get_user_context,
    set_skill_filter,
    set_user_context,
)


def test_user_context_round_trip() -> None:
    ctx = UserContext(username="alice", roles=["admin", "dev"])
    set_user_context(ctx)
    assert get_user_context() is ctx
    assert get_user_context().username == "alice"
    assert get_user_context().roles == ["admin", "dev"]


def test_user_context_none_when_unset() -> None:
    def _check() -> None:
        assert get_user_context() is None

    contextvars.Context().run(_check)


def test_skill_filter_round_trip() -> None:
    def f(name: str) -> bool:
        return name.startswith("web_")

    set_skill_filter(f)
    assert get_skill_filter() is f


def test_skill_filter_none_by_default() -> None:
    def _check() -> None:
        assert get_skill_filter() is None

    contextvars.Context().run(_check)


# --- ROLE_GATED_TOOLS tests ---


def test_role_gated_tools_contains_manage_users() -> None:
    assert "manage_users" in ROLE_GATED_TOOLS
    assert ROLE_GATED_TOOLS["manage_users"] == "admin"


def test_role_gate_passes_with_required_role() -> None:
    """User with admin role should pass the gate for manage_users."""
    user_ctx = UserContext(username="alice", roles=["admin", "dev"])
    required_role = ROLE_GATED_TOOLS["manage_users"]
    assert required_role in user_ctx.roles


def test_role_gate_blocks_without_required_role() -> None:
    """User without admin role should be blocked."""
    user_ctx = UserContext(username="bob", roles=["dev"])
    required_role = ROLE_GATED_TOOLS["manage_users"]
    assert required_role not in user_ctx.roles


def test_role_gate_blocks_when_no_user_context() -> None:
    """No user context (e.g., cron job) should be blocked."""

    def _check() -> None:
        user_ctx = get_user_context()
        required_role = ROLE_GATED_TOOLS["manage_users"]
        assert user_ctx is None or required_role not in (user_ctx.roles if user_ctx else [])

    contextvars.Context().run(_check)


# --- copy_context inheritance tests (mirrors subagent.py behaviour) ---


def test_user_context_inherited_in_copied_context() -> None:
    """UserContext set in parent is visible in child via copy_context()."""

    async def _run() -> None:
        set_user_context(UserContext(username="parent_user", roles=["admin"]))

        result: list[UserContext | None] = []

        async def child() -> None:
            result.append(get_user_context())

        # copy_context mirrors what subagent.py does
        await asyncio.create_task(child(), context=contextvars.copy_context())

        assert result[0] is not None
        assert result[0].username == "parent_user"
        assert result[0].roles == ["admin"]

    asyncio.run(_run())


def test_skill_filter_inherited_in_copied_context() -> None:
    """Skill filter set in parent is visible in child via copy_context()."""

    async def _run() -> None:
        my_filter = lambda name: name == "allowed"  # noqa: E731
        set_skill_filter(my_filter)

        result: list[object] = []

        async def child() -> None:
            result.append(get_skill_filter())

        await asyncio.create_task(child(), context=contextvars.copy_context())

        assert result[0] is my_filter

    asyncio.run(_run())
