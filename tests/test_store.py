from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from operator_ai.store import Store, User, _validate_username


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Create a Store backed by a temporary database."""
    return Store(path=tmp_path / "test.db")


# ── Username validation ─────────────────────────────────────


def test_valid_usernames() -> None:
    for name in ["alice", "bob.jones", "dev-1", "a", "a" * 64]:
        _validate_username(name)  # should not raise


def test_invalid_usernames() -> None:
    for name in ["", "Alice", "has space", "under_score", "a" * 65, "cafe\u0301", "UPPER"]:
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


# ── Conversations ───────────────────────────────────────────


def test_ensure_conversation_creates_and_updates(store: Store) -> None:
    store.ensure_conversation("conv-1", "slack", "C1", "T1")
    store.ensure_conversation("conv-1", "slack", "C1", "T1", metadata={"agent": "test"})
    # No error means upsert worked


def test_ensure_system_message_creates(store: Store) -> None:
    store.ensure_conversation("conv-1", "slack", "C1", "T1")
    store.ensure_system_message("conv-1", "Hello system")
    msgs = store.load_messages("conv-1")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "Hello system"


def test_ensure_system_message_updates(store: Store) -> None:
    store.ensure_conversation("conv-1", "slack", "C1", "T1")
    store.ensure_system_message("conv-1", "Hello system")
    store.ensure_system_message("conv-1", "Updated system")
    msgs = store.load_messages("conv-1")
    assert msgs[0]["content"] == "Updated system"


# ── Messages ────────────────────────────────────────────────


def test_append_and_load_messages(store: Store) -> None:
    store.ensure_conversation("conv-1", "slack", "C1", "T1")
    store.ensure_system_message("conv-1", "system")
    store.append_messages(
        "conv-1",
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
    )
    msgs = store.load_messages("conv-1")
    assert len(msgs) == 3
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"


def test_append_empty_messages(store: Store) -> None:
    store.ensure_conversation("conv-1", "slack", "C1", "T1")
    store.append_messages("conv-1", [])  # Should not error


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
    # Ensure the repair is persisted
    assert store.load_messages(conv) == loaded


def test_load_messages_preserves_created_at_metadata(store: Store) -> None:
    conv = "conv-ts"
    store.ensure_conversation(conv, "slack", "C1", "T1")
    store.ensure_system_message(conv, "system")
    store.append_messages(
        conv,
        [
            {
                "role": "user",
                "content": "u1",
                "_operator_created_at": "2026-03-09T15:29:41Z",
            }
        ],
    )

    loaded = store.load_messages(conv)
    assert loaded == [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": "u1",
            "_operator_created_at": "2026-03-09T15:29:41Z",
        },
    ]


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


# ── Platform message index ──────────────────────────────────


def test_index_and_lookup_platform_message(store: Store) -> None:
    store.ensure_conversation("conv-1", "slack", "C1", "T1")
    store.index_platform_message("slack", "msg-123", "conv-1")
    assert store.lookup_platform_message("slack", "msg-123") == "conv-1"


def test_lookup_unknown_platform_message(store: Store) -> None:
    assert store.lookup_platform_message("slack", "unknown") is None


def test_index_platform_message_upsert(store: Store) -> None:
    store.ensure_conversation("conv-1", "slack", "C1", "T1")
    store.ensure_conversation("conv-2", "slack", "C2", "T2")
    store.index_platform_message("slack", "msg-123", "conv-1")
    store.index_platform_message("slack", "msg-123", "conv-2")
    assert store.lookup_platform_message("slack", "msg-123") == "conv-2"


# ── Job state ───────────────────────────────────────────────


def test_load_job_state_default(store: Store) -> None:
    state = store.load_job_state("nonexistent-job")
    assert state.last_run == ""
    assert state.run_count == 0
    assert state.error_count == 0


def test_save_and_load_job_state(store: Store) -> None:
    from operator_ai.store import JobState

    state = JobState(
        last_run="2026-03-11T10:00:00Z",
        last_result="ok",
        last_duration_seconds=1.5,
        run_count=3,
    )
    store.save_job_state("my-job", state)
    loaded = store.load_job_state("my-job")
    assert loaded.last_run == "2026-03-11T10:00:00Z"
    assert loaded.last_result == "ok"
    assert loaded.last_duration_seconds == 1.5
    assert loaded.run_count == 3


def test_save_job_state_upsert(store: Store) -> None:
    from operator_ai.store import JobState

    store.save_job_state("my-job", JobState(run_count=1))
    store.save_job_state("my-job", JobState(run_count=2))
    loaded = store.load_job_state("my-job")
    assert loaded.run_count == 2


# ── Migration ───────────────────────────────────────────────


def test_store_migrates_legacy_messages_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            transport_name TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            root_thread_id TEXT NOT NULL,
            updated_at REAL NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            message_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    migrated = Store(path=db_path)

    columns = {
        row["name"] for row in migrated._conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    assert "created_at" in columns


# ── No legacy tables ────────────────────────────────────────


def test_no_memory_tables_exist(store: Store) -> None:
    """The new store should NOT have memories, vec_memories, memory_state, or agent_kv tables."""
    tables = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "memories" not in tables
    assert "vec_memories" not in tables
    assert "memory_state" not in tables
    assert "agent_kv" not in tables
    assert "schema_meta" not in tables


def test_no_kv_tables_exist(store: Store) -> None:
    """agent_kv table should not exist in the new store."""
    row = store._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_kv'"
    ).fetchone()
    assert row is None


# ── Context manager ─────────────────────────────────────────


def test_store_context_manager(tmp_path: Path) -> None:
    with Store(path=tmp_path / "ctx.db") as s:
        s.add_user("alice")
        assert s.get_user("alice") is not None
