from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from unittest.mock import patch

import pytest

from operator_ai.store import Store
from operator_ai.tools.context import UserContext, get_user_context, set_user_context
from operator_ai.tools.users import manage_users, set_timezone


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Create a Store backed by a temporary database."""
    return Store(path=tmp_path / "test.db")


def _run(coro):
    return asyncio.run(coro)


def _patched(store: Store):
    return patch("operator_ai.tools.users.get_store", return_value=store)


# ── list ─────────────────────────────────────────────────────


def test_list_no_users(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="list"))
    assert result == "No users."


def test_list_with_users(store: Store) -> None:
    store.add_user("alice")
    store.add_role("alice", "admin")
    store.add_identity("alice", "slack:U123")
    with _patched(store):
        result = _run(manage_users(action="list"))
    assert "alice" in result
    assert "admin" in result
    assert "slack:U123" in result


# ── add ──────────────────────────────────────────────────────


def test_add_user(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="add", username="alice", role="admin"))
    assert "Added user 'alice'" in result
    user = store.get_user("alice")
    assert user is not None
    assert "admin" in user.roles


def test_add_user_invalid_name(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="add", username="BAD NAME!", role="admin"))
    assert "[error:" in result


def test_add_user_missing_username(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="add", role="admin"))
    assert result == "[error: username is required]"


def test_add_user_missing_role(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="add", username="alice"))
    assert result == "[error: role is required]"


def test_add_user_duplicate(store: Store) -> None:
    store.add_user("alice")
    with _patched(store):
        result = _run(manage_users(action="add", username="alice", role="admin"))
    assert "[error:" in result
    assert "already exists" in result


# ── remove ───────────────────────────────────────────────────


def test_remove_user(store: Store) -> None:
    store.add_user("alice")
    with _patched(store):
        result = _run(manage_users(action="remove", username="alice"))
    assert "Removed user 'alice'" in result
    assert store.get_user("alice") is None


def test_remove_user_not_found(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="remove", username="ghost"))
    assert "[error:" in result
    assert "not found" in result


def test_remove_user_missing_username(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="remove"))
    assert result == "[error: username is required]"


# ── link ─────────────────────────────────────────────────────


def test_link_identity(store: Store) -> None:
    store.add_user("alice")
    with _patched(store):
        result = _run(
            manage_users(action="link", username="alice", transport="telegram", external_id="12345")
        )
    assert "Linked telegram:12345 to 'alice'" in result
    assert store.resolve_username("telegram:12345") == "alice"


def test_link_missing_transport(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="link", username="alice", external_id="12345"))
    assert result == "[error: transport is required]"


def test_link_missing_external_id(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="link", username="alice", transport="telegram"))
    assert result == "[error: external_id is required]"


def test_link_missing_username(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="link", transport="telegram", external_id="12345"))
    assert result == "[error: username is required]"


# ── unlink ───────────────────────────────────────────────────


def test_unlink_identity(store: Store) -> None:
    store.add_user("alice")
    store.add_identity("alice", "slack:U999")
    with _patched(store):
        result = _run(
            manage_users(action="unlink", username="alice", transport="slack", external_id="U999")
        )
    assert "Unlinked slack:U999" in result
    assert store.resolve_username("slack:U999") is None


def test_unlink_not_found(store: Store) -> None:
    with _patched(store):
        result = _run(
            manage_users(action="unlink", username="alice", transport="slack", external_id="nope")
        )
    assert "[error:" in result
    assert "not found" in result


def test_unlink_rejects_wrong_owner(store: Store) -> None:
    store.add_user("alice")
    store.add_user("gavin")
    store.add_identity("alice", "slack:U999")
    with _patched(store):
        result = _run(
            manage_users(action="unlink", username="gavin", transport="slack", external_id="U999")
        )
    assert "[error:" in result
    assert "belongs to 'alice'" in result


# ── add_role ─────────────────────────────────────────────────


def test_add_role(store: Store) -> None:
    store.add_user("alice")
    with _patched(store):
        result = _run(manage_users(action="add_role", username="alice", role="developer"))
    assert "Added role 'developer' to 'alice'" in result
    assert "developer" in store.get_user_roles("alice")


def test_add_role_missing_username(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="add_role", role="admin"))
    assert result == "[error: username is required]"


def test_add_role_missing_role(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="add_role", username="alice"))
    assert result == "[error: role is required]"


# ── remove_role ──────────────────────────────────────────────


def test_remove_role(store: Store) -> None:
    store.add_user("alice")
    store.add_role("alice", "admin")
    with _patched(store):
        result = _run(manage_users(action="remove_role", username="alice", role="admin"))
    assert "Removed role 'admin' from 'alice'" in result
    assert "admin" not in store.get_user_roles("alice")


def test_remove_role_not_found(store: Store) -> None:
    store.add_user("alice")
    with _patched(store):
        result = _run(manage_users(action="remove_role", username="alice", role="ghost"))
    assert "[error:" in result
    assert "not found" in result


def test_remove_role_missing_params(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="remove_role", username="alice"))
    assert result == "[error: role is required]"

    with _patched(store):
        result = _run(manage_users(action="remove_role", role="admin"))
    assert result == "[error: username is required]"


# ── unknown action ───────────────────────────────────────────


def test_unknown_action(store: Store) -> None:
    with _patched(store):
        result = _run(manage_users(action="explode"))
    assert "[error: unknown action" in result


# ── set_timezone ───────────────────────────────────────────


def test_set_timezone_updates_current_user(store: Store) -> None:
    store.add_user("alice")

    async def _call_async() -> tuple[str, str | None]:
        set_user_context(UserContext(username="alice", roles=["member"]))
        with _patched(store):
            result = await set_timezone("America/Vancouver")
        current = get_user_context()
        return result, current.timezone if current else None

    result, current_tz = contextvars.Context().run(lambda: _run(_call_async()))

    assert result == "Timezone set to America/Vancouver."
    assert store.get_user_timezone("alice") == "America/Vancouver"
    assert current_tz == "America/Vancouver"


def test_set_timezone_requires_user_context(store: Store) -> None:
    with _patched(store):
        result = contextvars.Context().run(lambda: _run(set_timezone("America/Vancouver")))
    assert result == "[error: timezone can only be set during a user conversation]"


def test_set_timezone_rejects_invalid_timezone(store: Store) -> None:
    store.add_user("alice")

    async def _call_async() -> str:
        set_user_context(UserContext(username="alice", roles=["member"]))
        with _patched(store):
            return await set_timezone("Mars/Olympus")

    result = contextvars.Context().run(lambda: _run(_call_async()))

    assert "[error: Unknown timezone:" in result
