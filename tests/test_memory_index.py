from __future__ import annotations

from pathlib import Path

import pytest

from operator_ai.memory_index import (
    MemoryIndex,
    _build_fts_query,
    _content_hash,
    _derive_scope_kind,
)


def test_content_hash_deterministic() -> None:
    assert _content_hash("hello") == _content_hash("hello")
    assert _content_hash("hello") != _content_hash("world")


def test_derive_scope_kind_global_notes() -> None:
    assert _derive_scope_kind("memory/global/notes/release-date.md") == ("global", "note")


def test_derive_scope_kind_global_rules() -> None:
    assert _derive_scope_kind("memory/global/rules/concise.md") == ("global", "rule")


def test_derive_scope_kind_agent_notes() -> None:
    assert _derive_scope_kind("agents/operator/memory/notes/foo.md") == ("agent:operator", "note")


def test_derive_scope_kind_agent_rules() -> None:
    assert _derive_scope_kind("agents/operator/memory/rules/bar.md") == ("agent:operator", "rule")


def test_derive_scope_kind_user_notes() -> None:
    assert _derive_scope_kind("memory/users/gavin/notes/pref.md") == ("user:gavin", "note")


def test_derive_scope_kind_user_rules() -> None:
    assert _derive_scope_kind("memory/users/gavin/rules/style.md") == ("user:gavin", "rule")


def test_derive_scope_kind_invalid() -> None:
    with pytest.raises(ValueError, match="Cannot derive"):
        _derive_scope_kind("random/path.md")


def test_build_fts_query() -> None:
    assert _build_fts_query("release date") == '"release"* "date"*'


def test_build_fts_query_strips_special_chars() -> None:
    q = _build_fts_query("what's the release-date?")
    assert "?" not in q
    assert "'" not in q


def test_build_fts_query_empty() -> None:
    assert _build_fts_query("") == ""
    assert _build_fts_query("   ") == ""


# ── MemoryIndex integration tests ────────────────────────────


@pytest.fixture()
def index(tmp_path: Path) -> MemoryIndex:
    idx = MemoryIndex(tmp_path / "test_index.db")
    yield idx
    idx.close()


def test_init_creates_tables(index: MemoryIndex) -> None:
    tables = index._conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'table')"
    ).fetchall()
    names = {r["name"] for r in tables}
    assert "memory_meta" in names
    assert "memory_fts" in names


def test_upsert_and_search(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/release-date.md",
        "global",
        "note",
        "release-date",
        "Release date moved to April 3",
        _content_hash("Release date moved to April 3"),
    )
    results = index.search("release", scopes=["global"], kind="note")
    assert len(results) == 1
    assert results[0].key == "release-date"


def test_upsert_updates_existing(index: MemoryIndex) -> None:
    path = "memory/global/notes/foo.md"
    index.upsert(path, "global", "note", "foo", "version 1", _content_hash("version 1"))
    index.upsert(path, "global", "note", "foo", "version 2", _content_hash("version 2"))

    # Should be only one row
    count = index._conn.execute(
        "SELECT COUNT(*) as n FROM memory_meta WHERE relative_path = ?", (path,)
    ).fetchone()["n"]
    assert count == 1

    # Search should find updated content
    results = index.search("version", scopes=["global"], kind="note")
    assert len(results) == 1


def test_delete_removes_from_fts(index: MemoryIndex) -> None:
    path = "memory/global/notes/ephemeral.md"
    index.upsert(path, "global", "note", "ephemeral", "temp data", _content_hash("temp data"))
    assert index.search("temp", scopes=["global"], kind="note")

    index.delete(path)
    assert not index.search("temp", scopes=["global"], kind="note")


def test_search_porter_stemming(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/deploy.md",
        "global",
        "note",
        "deploy",
        "The deployment process runs every Friday",
        _content_hash("The deployment process runs every Friday"),
    )
    # "deploying" should match "deployment" via Porter stemming
    results = index.search("deploying", scopes=["global"], kind="note")
    assert len(results) == 1
    assert results[0].key == "deploy"


def test_search_multi_scope(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/global-note.md",
        "global",
        "note",
        "global-note",
        "Global release info",
        _content_hash("Global release info"),
    )
    index.upsert(
        "agents/operator/memory/notes/agent-note.md",
        "agent:operator",
        "note",
        "agent-note",
        "Agent release info",
        _content_hash("Agent release info"),
    )

    # Search both scopes at once
    results = index.search("release", scopes=["global", "agent:operator"], kind="note")
    assert len(results) == 2
    keys = {r.key for r in results}
    assert keys == {"global-note", "agent-note"}


def test_search_respects_kind(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/deploy-note.md",
        "global",
        "note",
        "deploy-note",
        "Deploy on Friday",
        _content_hash("Deploy on Friday"),
    )
    index.upsert(
        "memory/global/rules/deploy-rule.md",
        "global",
        "rule",
        "deploy-rule",
        "Always deploy on Friday",
        _content_hash("Always deploy on Friday"),
    )

    notes = index.search("deploy", scopes=["global"], kind="note")
    assert len(notes) == 1
    assert notes[0].key == "deploy-note"

    rules = index.search("deploy", scopes=["global"], kind="rule")
    assert len(rules) == 1
    assert rules[0].key == "deploy-rule"


def test_get_content_hashes(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/a.md",
        "global",
        "note",
        "a",
        "content a",
        _content_hash("content a"),
    )
    index.upsert(
        "memory/global/notes/b.md",
        "global",
        "note",
        "b",
        "content b",
        _content_hash("content b"),
    )
    hashes = index.get_content_hashes()
    assert len(hashes) == 2
    assert hashes["memory/global/notes/a.md"] == _content_hash("content a")


def test_delete_missing(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/stale.md",
        "global",
        "note",
        "stale",
        "old data",
        _content_hash("old data"),
    )
    index.delete_missing({"memory/global/notes/stale.md"})
    assert index.count() == 0


def test_delete_expired(index: MemoryIndex) -> None:
    import time

    past = time.time() - 3600
    index.upsert(
        "memory/global/notes/expired.md",
        "global",
        "note",
        "expired",
        "old data",
        _content_hash("old data"),
        expires_at=past,
    )
    index.upsert(
        "memory/global/notes/active.md",
        "global",
        "note",
        "active",
        "fresh data",
        _content_hash("fresh data"),
    )

    swept = index.delete_expired()
    assert swept == 1
    assert index.count() == 1


def test_rebuild(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/x.md",
        "global",
        "note",
        "x",
        "data",
        _content_hash("data"),
    )
    assert index.count() == 1
    index.rebuild()
    assert index.count() == 0


def test_search_empty_query(index: MemoryIndex) -> None:
    index.upsert(
        "memory/global/notes/x.md",
        "global",
        "note",
        "x",
        "data",
        _content_hash("data"),
    )
    assert index.search("", scopes=["global"], kind="note") == []
    assert index.search("   ", scopes=["global"], kind="note") == []


def test_count(index: MemoryIndex) -> None:
    assert index.count() == 0
    index.upsert(
        "memory/global/notes/a.md",
        "global",
        "note",
        "a",
        "x",
        _content_hash("x"),
    )
    assert index.count() == 1
