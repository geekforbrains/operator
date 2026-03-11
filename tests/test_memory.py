from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from operator_ai.memory import (
    MemoryStore,
    _slugify,
    _write_memory_file,
    parse_ttl,
)
from operator_ai.tools import memory as memory_tools

# ── TTL parsing ──────────────────────────────────────────────


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


# ── Slugify ──────────────────────────────────────────────────


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


# ── Scope resolution ────────────────────────────────────────


def test_scope_global(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    d = store._scope_dir("global")
    assert d == tmp_path / "memory" / "global"


def test_scope_agent(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    d = store._scope_dir("agent:operator")
    assert d == tmp_path / "agents" / "operator" / "memory"


def test_scope_user(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    d = store._scope_dir("user:gavin")
    assert d == tmp_path / "memory" / "users" / "gavin"


def test_scope_invalid(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    with pytest.raises(ValueError, match="Invalid scope"):
        store._scope_dir("invalid")


# ── Create rule ──────────────────────────────────────────────


def test_create_rule(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_rule("global", "Be concise in answers.")
    assert path.startswith("memory/global/rules/")
    assert path.endswith(".md")
    full = tmp_path / path
    assert full.is_file()
    text = full.read_text()
    assert "Be concise in answers." in text
    assert "created_at:" in text


def test_create_rule_agent_scope(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_rule("agent:operator", "Use uv not pip")
    assert path.startswith("agents/operator/memory/rules/")


def test_create_rule_user_scope(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_rule("user:gavin", "Prefer short answers")
    assert path.startswith("memory/users/gavin/rules/")


# ── Create note ──────────────────────────────────────────────


def test_create_note(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "Release date moved to April 3.")
    assert path.startswith("memory/global/notes/")
    full = tmp_path / path
    assert full.is_file()
    text = full.read_text()
    assert "Release date moved to April 3." in text


def test_create_note_with_ttl(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "Temporary note", ttl="3d")
    full = tmp_path / path
    text = full.read_text()
    assert "expires_at:" in text


def test_create_note_dedup_filename(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    p1 = store.create_note("global", "Same content here")
    p2 = store.create_note("global", "Same content here")
    assert p1 != p2
    assert "-2" in p2


# ── List rules and notes ────────────────────────────────────


def test_list_rules(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_rule("global", "Rule one")
    store.create_rule("global", "Rule two")
    rules = store.list_rules("global")
    assert len(rules) == 2
    contents = {r.content for r in rules}
    assert "Rule one" in contents
    assert "Rule two" in contents


def test_list_rules_empty(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store.list_rules("global") == []


def test_list_notes(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_note("agent:test", "Note alpha")
    store.create_note("agent:test", "Note beta")
    notes = store.list_notes("agent:test")
    assert len(notes) == 2


# ── Search notes ─────────────────────────────────────────────


def test_search_notes_by_filename(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_note("global", "Release date moved to April 3")
    store.create_note("global", "Staging API base URL is https://staging.example.com")

    results = store.search_notes("global", "release")
    assert len(results) == 1
    assert "Release date" in results[0].content


def test_search_notes_by_content(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_note("global", "The project uses Python 3.12 with uv for packages")
    store.create_note("global", "Deployment happens on Fridays")

    results = store.search_notes("global", "python")
    assert len(results) >= 1
    assert any("Python 3.12" in r.content for r in results)


def test_search_notes_no_results(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_note("global", "Some note about testing")
    results = store.search_notes("global", "nonexistent-xyz")
    assert results == []


def test_search_notes_filename_match_first(tmp_path: Path) -> None:
    """Filename matches should appear before content-only matches."""
    store = MemoryStore(base_dir=tmp_path)
    # This note has "deploy" in content but not filename slug
    store.create_note("global", "We should deploy to production every Friday")
    # This note has "deploy" in the filename slug
    store.create_note("global", "Deploy process documentation")

    results = store.search_notes("global", "deploy")
    assert len(results) == 2
    # The one with "deploy" in the filename should come first
    assert "Deploy process" in results[0].content


# ── Read ─────────────────────────────────────────────────────


def test_read_memory(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "Test note content")
    mf = store.read(path)
    assert mf is not None
    assert mf.content == "Test note content"
    assert mf.relative_path == path
    assert mf.created_at is not None


def test_read_nonexistent(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store.read("nonexistent/path.md") is None


# ── Update ───────────────────────────────────────────────────


def test_update_memory(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "Original content")
    assert store.update(path, "Updated content")
    mf = store.read(path)
    assert mf is not None
    assert mf.content == "Updated content"


def test_update_preserves_created_at(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "Original")
    original = store.read(path)
    assert original is not None

    store.update(path, "Updated")
    updated = store.read(path)
    assert updated is not None
    assert updated.created_at == original.created_at
    # updated_at should be >= original
    assert updated.updated_at is not None
    assert updated.updated_at >= original.updated_at


def test_update_nonexistent(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store.update("nonexistent/path.md", "content") is False


# ── Forget ───────────────────────────────────────────────────


def test_forget_moves_to_trash(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "Ephemeral note")
    assert store.forget(path)
    # Original file should be gone
    assert not (tmp_path / path).exists()
    # Trash directory should have the file
    trash_dir = tmp_path / "memory" / "global" / "trash"
    assert trash_dir.is_dir()
    trash_files = list(trash_dir.glob("*.md"))
    assert len(trash_files) == 1


def test_forget_nonexistent(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    assert store.forget("nonexistent/path.md") is False


def test_forget_rule(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_rule("global", "A rule to forget")
    assert store.forget(path)
    trash_dir = tmp_path / "memory" / "global" / "trash"
    assert list(trash_dir.glob("*.md"))


# ── Sweep expired ────────────────────────────────────────────


def test_sweep_expired(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)

    # Create an expired note manually
    notes_dir = tmp_path / "memory" / "global" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    expired_time = datetime.now(UTC) - timedelta(hours=1)
    _write_memory_file(
        notes_dir / "expired-note.md",
        "This has expired",
        expires_at=expired_time,
    )

    # Create a non-expired note
    store.create_note("global", "This is still valid")

    count = store.sweep_expired()
    assert count == 1

    # Expired file should be in trash
    trash_dir = tmp_path / "memory" / "global" / "trash"
    assert len(list(trash_dir.glob("*.md"))) == 1

    # Valid note should still exist
    notes = store.list_notes("global")
    assert len(notes) == 1
    assert "still valid" in notes[0].content


def test_sweep_expired_no_expired(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_note("global", "Not expired")
    assert store.sweep_expired() == 0


# ── Memory tools ─────────────────────────────────────────────


def _configure_memory_tools(
    store: MemoryStore,
    agent_name: str = "operator",
    user_id: str = "",
    allow_user_scope: bool = True,
) -> None:
    """Helper to configure memory tools context for tests."""
    memory_tools.configure(
        {
            "memory_store": store,
            "agent_name": agent_name,
            "user_id": user_id,
            "allow_user_scope": allow_user_scope,
        }
    )


def test_tool_remember_rule(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.remember_rule("Be concise", scope="agent"))
    assert "Rule saved:" in result
    assert "agents/operator/memory/rules/" in result


def test_tool_remember_note(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.remember_note("Release is April 3", scope="global"))
    assert "Note saved:" in result


def test_tool_remember_note_with_ttl(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.remember_note("Temporary fact", scope="agent", ttl="3d"))
    assert "expires in 3d" in result


def test_tool_search_notes(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_note("agent:operator", "Release date is April 3")
    store.create_note("agent:operator", "Deployment is every Friday")

    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.search_notes("release", scope="agent"))
    assert "April 3" in result


def test_tool_list_rules(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_rule("agent:operator", "Be concise")
    store.create_rule("agent:operator", "Use uv not pip")

    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.list_rules(scope="agent"))
    assert "Be concise" in result
    assert "uv not pip" in result


def test_tool_list_notes(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.create_note("global", "Global note content")

    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.list_notes(scope="global"))
    assert "Global note content" in result


def test_tool_update_memory(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "Original")
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.update_memory(path, "Updated content"))
    assert "Updated:" in result


def test_tool_forget_memory(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    path = store.create_note("global", "To be forgotten")
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.forget_memory(path))
    assert "Moved to trash:" in result


def test_tool_scope_resolution_user(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store, user_id="gavin", allow_user_scope=True)
    result = asyncio.run(memory_tools.remember_rule("User preference", scope="user"))
    assert "memory/users/gavin/rules/" in result


def test_tool_scope_resolution_user_missing_username(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store, user_id="", allow_user_scope=True)
    result = asyncio.run(memory_tools.remember_rule("User preference", scope="user"))
    assert "[error:" in result


def test_tool_invalid_scope(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    _configure_memory_tools(store)
    result = asyncio.run(memory_tools.remember_rule("Something", scope="invalid"))
    assert "[error:" in result
