from __future__ import annotations

from pathlib import Path

import pytest

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from operator_ai.store import Store, User, _validate_username, serialize_float32


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Create a Store backed by a temporary database."""
    return Store(path=tmp_path / "test.db")


# ── Username validation ─────────────────────────────────────


def test_valid_usernames() -> None:
    for name in ["alice", "bob.jones", "dev-1", "a", "a" * 64]:
        _validate_username(name)  # should not raise


def test_invalid_usernames() -> None:
    for name in ["", "Alice", "has space", "under_score", "a" * 65, "café", "UPPER"]:
        with pytest.raises(ValueError, match="Invalid username"):
            _validate_username(name)


# ── add_user / get_user / list_users / remove_user ──────────


def test_add_and_get_user(store: Store) -> None:
    store.add_user("alice")
    user = store.get_user("alice")
    assert user is not None
    assert user.username == "alice"
    assert user.created_at  # non-empty
    assert user.identities == []
    assert user.roles == []


def test_add_user_invalid_raises(store: Store) -> None:
    with pytest.raises(ValueError, match="Invalid username"):
        store.add_user("BAD NAME!")


def test_add_user_duplicate_raises(store: Store) -> None:
    store.add_user("alice")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_user("alice")


def test_get_user_nonexistent(store: Store) -> None:
    assert store.get_user("ghost") is None


def test_list_users(store: Store) -> None:
    store.add_user("bob")
    store.add_user("alice")
    users = store.list_users()
    assert [u.username for u in users] == ["alice", "bob"]  # sorted


def test_list_users_empty(store: Store) -> None:
    assert store.list_users() == []


def test_remove_user(store: Store) -> None:
    store.add_user("alice")
    assert store.remove_user("alice") is True
    assert store.get_user("alice") is None


def test_remove_user_nonexistent(store: Store) -> None:
    assert store.remove_user("ghost") is False


# ── Identities ──────────────────────────────────────────────


def test_add_and_resolve_identity(store: Store) -> None:
    store.add_user("alice")
    store.add_identity("alice", "slack:U04ABC123")
    assert store.resolve_username("slack:U04ABC123") == "alice"


def test_resolve_unknown_identity(store: Store) -> None:
    assert store.resolve_username("telegram:99999") is None


def test_remove_identity(store: Store) -> None:
    store.add_user("alice")
    store.add_identity("alice", "slack:U04ABC123")
    assert store.remove_identity("slack:U04ABC123") is True
    assert store.resolve_username("slack:U04ABC123") is None


def test_remove_identity_nonexistent(store: Store) -> None:
    assert store.remove_identity("slack:nope") is False


def test_get_user_includes_identities(store: Store) -> None:
    store.add_user("alice")
    store.add_identity("alice", "slack:U04ABC123")
    store.add_identity("alice", "telegram:12345678")
    user = store.get_user("alice")
    assert user is not None
    assert sorted(user.identities) == ["slack:U04ABC123", "telegram:12345678"]


# ── Roles ───────────────────────────────────────────────────


def test_add_and_get_roles(store: Store) -> None:
    store.add_user("alice")
    store.add_role("alice", "admin")
    store.add_role("alice", "developer")
    assert store.get_user_roles("alice") == ["admin", "developer"]  # sorted


def test_get_user_roles_empty(store: Store) -> None:
    store.add_user("alice")
    assert store.get_user_roles("alice") == []


def test_remove_role(store: Store) -> None:
    store.add_user("alice")
    store.add_role("alice", "admin")
    assert store.remove_role("alice", "admin") is True
    assert store.get_user_roles("alice") == []


def test_remove_role_nonexistent(store: Store) -> None:
    store.add_user("alice")
    assert store.remove_role("alice", "admin") is False


def test_get_user_includes_roles(store: Store) -> None:
    store.add_user("alice")
    store.add_role("alice", "admin")
    user = store.get_user("alice")
    assert user is not None
    assert user.roles == ["admin"]


# ── Cascade on remove_user ──────────────────────────────────


def test_remove_user_cascades_identities(store: Store) -> None:
    store.add_user("alice")
    store.add_identity("alice", "slack:U04ABC123")
    store.add_identity("alice", "telegram:12345678")
    store.remove_user("alice")
    assert store.resolve_username("slack:U04ABC123") is None
    assert store.resolve_username("telegram:12345678") is None


def test_remove_user_cascades_roles(store: Store) -> None:
    store.add_user("alice")
    store.add_role("alice", "admin")
    store.add_role("alice", "developer")
    store.remove_user("alice")
    assert store.get_user_roles("alice") == []


def test_remove_user_cascades_both(store: Store) -> None:
    store.add_user("alice")
    store.add_identity("alice", "slack:U04ABC123")
    store.add_role("alice", "admin")
    store.remove_user("alice")
    assert store.resolve_username("slack:U04ABC123") is None
    assert store.get_user_roles("alice") == []
    assert store.get_user("alice") is None


# ── User dataclass ──────────────────────────────────────────


def test_user_dataclass() -> None:
    u = User(username="alice", created_at="2024-01-01", identities=["slack:X"], roles=["admin"])
    assert u.username == "alice"
    assert u.identities == ["slack:X"]
    assert u.roles == ["admin"]


# ── Memory retention / expiry ───────────────────────────────


def test_list_memories_excludes_expired_candidates(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "memory.db", embed_dimensions=3)
    vec = serialize_float32([1.0, 0.0, 0.0])
    store.insert_memory(
        "durable note",
        "agent",
        "operator",
        vec,
        retention="durable",
    )
    store.insert_memory(
        "expired note",
        "agent",
        "operator",
        vec,
        retention="candidate",
        expires_at="2000-01-01T00:00:00Z",
    )

    rows = store.list_memories("agent", "operator")

    assert [row["content"] for row in rows] == ["durable note"]


def test_search_memories_excludes_expired_candidates(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "memory.db", embed_dimensions=3)
    vec = serialize_float32([1.0, 0.0, 0.0])
    store.insert_memory(
        "durable note",
        "agent",
        "operator",
        vec,
        retention="durable",
    )
    store.insert_memory(
        "expired note",
        "agent",
        "operator",
        vec,
        retention="candidate",
        expires_at="2000-01-01T00:00:00Z",
    )

    rows = store.search_memories_vec(vec, "agent", "operator", top_k=5)

    assert [row["content"] for row in rows] == ["durable note"]


def test_sweep_expired_memories_removes_candidate_rows(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "memory.db", embed_dimensions=3)
    vec = serialize_float32([1.0, 0.0, 0.0])
    store.insert_memory(
        "expired note",
        "agent",
        "operator",
        vec,
        retention="candidate",
        expires_at="2000-01-01T00:00:00Z",
    )

    removed = store.sweep_expired_memories()

    assert removed == 1
    assert store.list_memories("agent", "operator") == []


def test_load_messages_trims_incomplete_tool_turns(store: Store) -> None:
    conv = "conv-1"
    store.ensure_conversation(conv, "slack", "C1", "T1")
    store.ensure_system_message(conv, "system")
    store.append_messages(
        conv,
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1", "tool_calls": [{"id": "call_1"}]},
            {"role": "user", "content": "u2"},
        ],
    )

    loaded = store.load_messages(conv)

    assert loaded == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "u1"},
    ]
    # Ensure the repair is persisted in SQLite.
    assert store.load_messages(conv) == loaded


def test_load_messages_keeps_complete_tool_turns(store: Store) -> None:
    conv = "conv-2"
    store.ensure_conversation(conv, "slack", "C1", "T1")
    store.ensure_system_message(conv, "system")
    store.append_messages(
        conv,
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "t1"},
            {"role": "assistant", "content": "done"},
        ],
    )

    loaded = store.load_messages(conv)

    assert loaded == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "t1"},
        {"role": "assistant", "content": "done"},
    ]
