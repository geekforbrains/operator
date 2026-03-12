from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from operator_ai.memory import MemoryStore, _slugify, _write_memory_file, parse_ttl
from operator_ai.memory_index import MemoryIndex
from operator_ai.memory_reindex import reindex_diff, reindex_full
from operator_ai.tools import memory as memory_tools


def test_parse_ttl_minutes() -> None:
    assert parse_ttl("30m") == timedelta(minutes=30)


def test_parse_ttl_hours() -> None:
    assert parse_ttl("1h") == timedelta(hours=1)


def test_parse_ttl_days() -> None:
    assert parse_ttl("3d") == timedelta(days=3)


def test_parse_ttl_weeks() -> None:
    assert parse_ttl("2w") == timedelta(weeks=2)


def test_parse_ttl_with_whitespace() -> None:
    assert parse_ttl("  7d  ") == timedelta(days=7)


def test_parse_ttl_invalid() -> None:
    with pytest.raises(ValueError, match="Invalid TTL format"):
        parse_ttl("abc")


def test_parse_ttl_invalid_no_unit() -> None:
    with pytest.raises(ValueError, match="Invalid TTL format"):
        parse_ttl("42")


def test_slugify_basic() -> None:
    assert _slugify("Release date moved to April 3") == "release-date-moved-to-april-3"


def test_slugify_special_chars() -> None:
    assert _slugify("Use `uv` rather than `pip`") == "use-uv-rather-than-pip"


def test_slugify_truncates() -> None:
    long_text = "a" * 100
    slug = _slugify(long_text, max_len=60)
    assert len(slug) <= 60


def test_slugify_empty() -> None:
    assert _slugify("") == "untitled"


def test_slugify_only_special_chars() -> None:
    assert _slugify("!@#$%^&*()") == "untitled"


def test_scope_global(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store._scope_dir("global") == tmp_path / "memory" / "global"


def test_scope_agent(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store._scope_dir("agent:operator") == tmp_path / "agents" / "operator" / "memory"


def test_scope_user(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store._scope_dir("user:gavin") == tmp_path / "memory" / "users" / "gavin"


def test_scope_invalid(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid scope"):
        store._scope_dir("invalid")


def test_upsert_rule_creates_deterministic_path(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.upsert_rule("global", "response-style", "Be concise in answers.")
    assert path == "memory/global/rules/response-style.md"
    text = (tmp_path / path).read_text()
    assert "Be concise in answers." in text
    assert "created_at:" in text


def test_upsert_rule_agent_scope(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.upsert_rule("agent:operator", "tooling-preference", "Use uv not pip")
    assert path == "agents/operator/memory/rules/tooling-preference.md"


def test_upsert_note_user_scope(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.upsert_note("user:gavin", "travel-plan", "Traveling this week")
    assert path == "memory/users/gavin/notes/travel-plan.md"


def test_upsert_note_normalizes_key(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.upsert_note("global", "Release Date", "Release date moved to April 3.")
    assert path == "memory/global/notes/release-date.md"


def test_upsert_same_key_updates_in_place(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path_1 = store.upsert_note("global", "release-date", "April 3")
    original = store.get_note("global", "release-date")
    assert original is not None

    path_2 = store.upsert_note("global", "release-date", "April 10", ttl="3d")
    updated = store.get_note("global", "release-date")
    assert updated is not None

    assert path_1 == path_2
    assert updated.content == "April 10"
    assert updated.created_at == original.created_at
    assert updated.updated_at is not None
    assert original.updated_at is not None
    assert updated.updated_at >= original.updated_at
    assert updated.expires_at is not None


def test_upsert_note_without_ttl_clears_existing_expiry(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_note("global", "travel-plan", "Travel this week", ttl="3d")
    store.upsert_note("global", "travel-plan", "No longer traveling")
    note = store.get_note("global", "travel-plan")
    assert note is not None
    assert note.expires_at is None


def test_upsert_invalid_key_raises(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid memory key"):
        store.upsert_note("global", "!!!", "content")


def test_list_rules(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("global", "rule-one", "Rule one")
    store.upsert_rule("global", "rule-two", "Rule two")
    rules = store.list_rules("global")
    assert [r.key for r in rules] == ["rule-one", "rule-two"]
    assert {r.content for r in rules} == {"Rule one", "Rule two"}


def test_list_notes(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_note("agent:test", "note-alpha", "Note alpha")
    store.upsert_note("agent:test", "note-beta", "Note beta")
    notes = store.list_notes("agent:test")
    assert [n.key for n in notes] == ["note-alpha", "note-beta"]


def test_get_rule(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("global", "response-style", "Prefer concise answers.")
    mf = store.get_rule("global", "response-style")
    assert mf is not None
    assert mf.key == "response-style"
    assert mf.content == "Prefer concise answers."


def test_get_note_missing_returns_none(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store.get_note("global", "missing") is None


def test_expired_note_hidden_from_get_list_and_search(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    notes_dir = tmp_path / "memory" / "global" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    _write_memory_file(
        notes_dir / "travel-plan.md",
        "Traveling this week",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert store.get_note("global", "travel-plan") is None
    assert store.list_notes("global") == []
    assert store.search_notes("global", "travel") == []


def test_expired_rule_hidden_from_list_and_get(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    rules_dir = tmp_path / "memory" / "global" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    _write_memory_file(
        rules_dir / "temporary-style.md",
        "This should not remain active",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert store.get_rule("global", "temporary-style") is None
    assert store.list_rules("global") == []


def test_search_notes_by_key(tmp_path: Path) -> None:
    index = MemoryIndex(tmp_path / "db" / "index.db")
    store = MemoryStore(base_dir=tmp_path, index=index)
    store.upsert_note("global", "release-date", "Release date moved to April 3")
    store.upsert_note(
        "global", "staging-api", "Staging API base URL is https://staging.example.com"
    )

    results = store.search_notes("global", "release")
    assert len(results) == 1
    assert results[0].key == "release-date"
    index.close()


def test_search_notes_by_content(tmp_path: Path) -> None:
    index = MemoryIndex(tmp_path / "db" / "index.db")
    store = MemoryStore(base_dir=tmp_path, index=index)
    store.upsert_note("global", "tooling", "The project uses Python 3.12 with uv for packages")
    store.upsert_note("global", "deploy-day", "Deployment happens on Fridays")

    results = store.search_notes("global", "python")
    assert len(results) == 1
    assert results[0].key == "tooling"
    index.close()


def test_search_notes_matches_across_notes(tmp_path: Path) -> None:
    index = MemoryIndex(tmp_path / "db" / "index.db")
    store = MemoryStore(base_dir=tmp_path, index=index)
    store.upsert_note("global", "release-process", "Release process runs every Friday")
    store.upsert_note("global", "deploy-guidelines", "General documentation about releases")

    results = store.search_notes("global", "release")
    assert len(results) == 2
    index.close()


def test_forget_note_moves_to_trash(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.upsert_note("global", "ephemeral-note", "Ephemeral note")
    assert store.forget_note("global", "ephemeral-note")
    assert not (tmp_path / path).exists()
    trash_dir = tmp_path / "memory" / "global" / "trash"
    assert list(trash_dir.glob("ephemeral-note*.md"))


def test_forget_rule_moves_to_trash(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.upsert_rule("global", "response-style", "Be concise")
    assert store.forget_rule("global", "response-style")
    assert not (tmp_path / path).exists()
    trash_dir = tmp_path / "memory" / "global" / "trash"
    assert list(trash_dir.glob("response-style*.md"))


def test_forget_missing_returns_false(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store.forget_note("global", "missing") is False


def test_sweep_expired(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)

    notes_dir = tmp_path / "memory" / "global" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    _write_memory_file(
        notes_dir / "expired-note.md",
        "This has expired",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    rules_dir = tmp_path / "memory" / "global" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    _write_memory_file(
        rules_dir / "expired-rule.md",
        "This rule has expired",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    store.upsert_note("global", "still-valid", "This is still valid")

    count = store.sweep_expired()
    assert count == 2
    trash_dir = tmp_path / "memory" / "global" / "trash"
    assert len(list(trash_dir.glob("*.md"))) == 2

    notes = store.list_notes("global")
    assert len(notes) == 1
    assert notes[0].key == "still-valid"


def test_sweep_expired_no_expired(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_note("global", "still-valid", "Not expired")
    assert store.sweep_expired() == 0


def _configure_memory_tools(
    store: MemoryStore,
    agent_name: str = "operator",
    username: str = "",
    allow_user_scope: bool = True,
) -> None:
    memory_tools.configure(
        {
            "memory_store": store,
            "agent_name": agent_name,
            "username": username,
            "allow_user_scope": allow_user_scope,
        }
    )


def test_tool_save_rule(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.save_rule("response-style", "Be concise", scope="agent"))
    assert result == "Saved rule 'response-style' in agent scope."
    saved = store.get_rule("agent:operator", "response-style")
    assert saved is not None


def test_tool_save_note(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(
        memory_tools.save_note("release-date", "Release is April 3", scope="global")
    )
    assert result == "Saved note 'release-date' in global scope."


def test_tool_save_note_with_ttl(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(
        memory_tools.save_note("travel-plan", "Temporary fact", scope="agent", ttl="3d")
    )
    assert result == "Saved note 'travel-plan' in agent scope (expires in 3d)."


def test_tool_search_notes_returns_keys_not_paths(tmp_path: Path) -> None:
    index = MemoryIndex(tmp_path / "db" / "index.db")
    store = MemoryStore(base_dir=tmp_path, index=index)
    store.upsert_note("agent:operator", "release-date", "Release date is April 3")
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.search_notes("release", scope="agent"))
    assert "[release-date]" in result
    assert "memory/" not in result
    index.close()


def test_tool_list_rules_returns_keys_not_paths(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("agent:operator", "response-style", "Be concise")
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.list_rules(scope="agent"))
    assert "[response-style]" in result
    assert "memory/" not in result


def test_tool_list_notes_returns_keys_not_paths(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_note("global", "release-date", "Global note content")
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.list_notes(scope="global"))
    assert "[release-date]" in result
    assert "memory/" not in result


def test_tool_list_notes_pagination(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    for i in range(5):
        store.upsert_note("global", f"note-{i}", f"Content {i}")
    _configure_memory_tools(store)

    # limit=3 should show 3 notes + continuation hint
    result = asyncio.run(memory_tools.list_notes(scope="global", limit=3))
    assert result.count("[note-") == 3
    assert "2 more" in result

    # offset=3 should show remaining 2
    result = asyncio.run(memory_tools.list_notes(scope="global", limit=3, offset=3))
    assert result.count("[note-") == 2
    assert "more" not in result

    # offset past end
    result = asyncio.run(memory_tools.list_notes(scope="global", limit=3, offset=10))
    assert "No notes at offset 10" in result


def test_tool_forget_rule(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("global", "response-style", "Be concise")
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.forget_rule("response-style", scope="global"))
    assert result == "Moved rule 'response-style' to trash in global scope."


def test_tool_forget_note(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_note("global", "release-date", "To be forgotten")
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.forget_note("release-date", scope="global"))
    assert result == "Moved note 'release-date' to trash in global scope."


def test_tool_user_scope_write_allowed_in_private(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store, username="gavin", allow_user_scope=True)
    result = asyncio.run(memory_tools.save_rule("response-style", "User preference", scope="user"))
    assert result == "Saved rule 'response-style' in user scope."


def test_tool_user_scope_write_blocked_outside_private(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store, username="gavin", allow_user_scope=False)
    result = asyncio.run(memory_tools.save_rule("response-style", "User preference", scope="user"))
    assert result == "[error: user-scoped memory is only available in private conversations]"


def test_tool_user_scope_read_blocked_outside_private(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_note("user:gavin", "travel-plan", "Private note")
    _configure_memory_tools(store, username="gavin", allow_user_scope=False)
    result = asyncio.run(memory_tools.search_notes("travel", scope="user"))
    assert result == "[error: user-scoped memory is only available in private conversations]"


def test_tool_user_scope_list_blocked_outside_private(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_note("user:gavin", "travel-plan", "Private note")
    _configure_memory_tools(store, username="gavin", allow_user_scope=False)
    result = asyncio.run(memory_tools.list_notes(scope="user"))
    assert result == "[error: user-scoped memory is only available in private conversations]"


def test_tool_invalid_scope(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.save_rule("response-style", "Something", scope="invalid"))
    assert "[error:" in result


# ── Indexed search integration tests ────────────────────────────


def _make_indexed_store(tmp_path: Path) -> tuple[MemoryStore, MemoryIndex]:
    index = MemoryIndex(tmp_path / "db" / "index.db")
    store = MemoryStore(base_dir=tmp_path, index=index)
    return store, index


def test_indexed_upsert_and_search(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    store.upsert_note("global", "release-date", "Release date moved to April 3")
    results = store.search_notes("global", "release")
    assert len(results) == 1
    assert results[0].key == "release-date"
    index.close()


def test_indexed_search_porter_stemming(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    store.upsert_note("global", "deploy-process", "The deployment happens on Fridays")
    # "deploying" should match "deployment" via Porter stemming
    results = store.search_notes("global", "deploying")
    assert len(results) == 1
    assert results[0].key == "deploy-process"
    index.close()


def test_indexed_forget_removes_from_search(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    store.upsert_note("global", "ephemeral", "Temporary data")
    assert store.search_notes("global", "temporary")
    store.forget_note("global", "ephemeral")
    assert not store.search_notes("global", "temporary")
    index.close()


def test_indexed_sweep_cleans_index(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    notes_dir = tmp_path / "memory" / "global" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    _write_memory_file(
        notes_dir / "expired.md",
        "This has expired",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    # Manually index the expired file
    index.upsert(
        "memory/global/notes/expired.md",
        "global",
        "note",
        "expired",
        "This has expired",
        "hash123",
        expires_at=(datetime.now(UTC) - timedelta(hours=1)).timestamp(),
    )
    store.upsert_note("global", "active", "Still active")
    assert index.count() == 2
    store.sweep_expired()
    assert index.count() == 1
    index.close()


def test_reindex_diff_picks_up_new_files(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    # Write files directly (simulating human edits)
    notes_dir = tmp_path / "memory" / "global" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    _write_memory_file(notes_dir / "manual-note.md", "Manually created note")

    upserted, deleted = reindex_diff(store, index)
    assert upserted == 1
    assert deleted == 0

    results = index.search("manual", scopes=["global"], kind="note")
    assert len(results) == 1
    index.close()


def test_reindex_diff_removes_deleted_files(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    # Index a file that doesn't exist on disk
    index.upsert(
        "memory/global/notes/ghost.md",
        "global",
        "note",
        "ghost",
        "phantom data",
        "hash000",
    )
    assert index.count() == 1

    _upserted, deleted = reindex_diff(store, index)
    assert deleted == 1
    assert index.count() == 0
    index.close()


def test_reindex_diff_skips_unchanged(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    store.upsert_note("global", "stable-note", "Content that won't change")
    assert index.count() == 1

    # Reindex again — nothing should change
    upserted, deleted = reindex_diff(store, index)
    assert upserted == 0
    assert deleted == 0
    index.close()


def test_reindex_full_rebuilds(tmp_path: Path) -> None:
    store, index = _make_indexed_store(tmp_path)
    store.upsert_note("global", "note-a", "First note")
    store.upsert_note("global", "note-b", "Second note")

    count = reindex_full(store, index)
    assert count == 2
    assert index.count() == 2
    index.close()
