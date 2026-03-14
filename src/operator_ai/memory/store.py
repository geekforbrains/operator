"""File-backed memory store.

Files are the source of truth. When an index is provided, writes and deletes
are mirrored into the FTS5/vector index synchronously.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from operator_ai.config import OPERATOR_DIR
from operator_ai.frontmatter import extract_body, parse_frontmatter

if TYPE_CHECKING:
    from operator_ai.memory.index import MemoryIndex

logger = logging.getLogger("operator.memory")

_TTL_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)
_KIND_DIRS = {"rule": "rules", "note": "notes"}


def parse_ttl(ttl: str) -> timedelta:
    """Parse a human-friendly duration string into a timedelta.

    Supported formats: ``"30m"`` (minutes), ``"1h"`` (hours),
    ``"3d"`` (days), ``"2w"`` (weeks).
    """
    m = _TTL_RE.match(ttl.strip())
    if not m:
        raise ValueError(f"Invalid TTL format: {ttl!r} (expected e.g. '3d', '2w', '1h', '30m')")
    value = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    if unit == "w":
        return timedelta(weeks=value)
    raise ValueError(f"Unknown TTL unit: {unit!r}")  # pragma: no cover


def _normalize_key(key: str, max_len: int = 80) -> str:
    """Normalize a memory key into a stable filename-safe slug."""
    if not re.search(r"[a-z0-9]", key, re.IGNORECASE):
        raise ValueError(f"Invalid memory key: {key!r}")
    text = key[:max_len].lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-") or "untitled"


def _unique_path(directory: Path, slug: str) -> Path:
    """Return a unique .md path in *directory* based on *slug*."""
    candidate = directory / f"{slug}.md"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = directory / f"{slug}-{counter}.md"
        if not candidate.exists():
            return candidate
        counter += 1


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_memory_file(
    path: Path,
    content: str,
    *,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Write a memory file with YAML frontmatter."""
    now = _now_utc()
    fm: dict[str, Any] = {
        "created_at": _format_dt(created_at or now),
        "updated_at": _format_dt(updated_at or now),
    }
    if expires_at is not None:
        fm["expires_at"] = _format_dt(expires_at)

    path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.dump(fm, default_flow_style=False).rstrip()
    path.write_text(f"---\n{fm_text}\n---\n\n{content.rstrip()}\n")


def _parse_memory_file(path: Path, relative_to: Path) -> MemoryFile | None:
    """Parse a memory file, returning None on read errors."""
    try:
        text = path.read_text()
    except OSError:
        logger.warning("could not read memory file: %s", path)
        return None

    fm = parse_frontmatter(text)
    content = extract_body(text)

    created_at = None
    updated_at = None
    expires_at = None
    if fm:
        created_at = _parse_dt(fm.get("created_at"))
        updated_at = _parse_dt(fm.get("updated_at"))
        expires_at = _parse_dt(fm.get("expires_at"))

    return MemoryFile(
        path=path,
        relative_path=str(path.relative_to(relative_to)),
        key=path.stem,
        created_at=created_at,
        updated_at=updated_at,
        expires_at=expires_at,
        content=content,
    )


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _resolve_scope(scope: str, base_dir: Path) -> Path:
    """Resolve a scope string to a directory path."""
    if scope == "global":
        return base_dir / "memory" / "global"
    if scope.startswith("agent:"):
        return base_dir / "agents" / scope[6:] / "memory"
    if scope.startswith("user:"):
        return base_dir / "memory" / "users" / scope[5:]
    raise ValueError(f"Invalid scope: {scope!r}")


def _is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False
    return expires_at <= (now or _now_utc())


@dataclass
class MemoryFile:
    path: Path
    relative_path: str
    key: str
    created_at: datetime | None
    updated_at: datetime | None
    expires_at: datetime | None
    content: str


class MemoryStore:
    """File-backed memory store with optional FTS5 index.

    Files remain the source of truth. When an index is provided, writes
    and deletes are mirrored into the FTS5/vector index synchronously.
    """

    def __init__(self, base_dir: Path = OPERATOR_DIR, *, index: MemoryIndex | None = None):
        self.base_dir = base_dir
        self._index = index

    def _scope_dir(self, scope: str) -> Path:
        return _resolve_scope(scope, self.base_dir)

    def _memory_dir(self, scope: str, kind: str) -> Path:
        try:
            subdir = _KIND_DIRS[kind]
        except KeyError:
            raise ValueError(f"Invalid memory kind: {kind!r}") from None
        return self._scope_dir(scope) / subdir

    def _memory_path(self, scope: str, kind: str, key: str) -> Path:
        return self._memory_dir(scope, kind) / f"{_normalize_key(key)}.md"

    def _read_path(self, path: Path, *, include_expired: bool = False) -> MemoryFile | None:
        if not path.is_file():
            return None
        mf = _parse_memory_file(path, self.base_dir)
        if mf is None:
            return None
        if not include_expired and _is_expired(mf.expires_at):
            return None
        return mf

    def _upsert(
        self,
        scope: str,
        kind: str,
        key: str,
        content: str,
        *,
        ttl: str | None = None,
    ) -> str:
        path = self._memory_path(scope, kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)

        existing = self._read_path(path, include_expired=True)
        now = _now_utc()
        expires_at = now + parse_ttl(ttl) if ttl else None
        created_at = existing.created_at if existing else now
        write_memory_file(
            path,
            content,
            created_at=created_at,
            updated_at=now,
            expires_at=expires_at,
        )
        relative = str(path.relative_to(self.base_dir))
        logger.info("saved %s: %s", kind, relative)

        if self._index:
            from operator_ai.memory.index import content_hash

            normalized_key = _normalize_key(key)
            self._index.upsert(
                relative,
                scope,
                kind,
                normalized_key,
                content,
                content_hash(content),
                created_at=created_at.timestamp() if created_at else None,
                updated_at=now.timestamp(),
                expires_at=expires_at.timestamp() if expires_at else None,
            )
            self._index.embed(relative, content)

        return relative

    def upsert_rule(self, scope: str, key: str, content: str) -> str:
        """Create or replace a rule file and return its relative path."""
        return self._upsert(scope, "rule", key, content)

    def upsert_note(self, scope: str, key: str, content: str, ttl: str | None = None) -> str:
        """Create or replace a note file and return its relative path."""
        return self._upsert(scope, "note", key, content, ttl=ttl)

    # ── List ─────────────────────────────────────────────────────

    def list_rules(self, scope: str) -> list[MemoryFile]:
        return self._list_files(self._memory_dir(scope, "rule"))

    def list_notes(self, scope: str) -> list[MemoryFile]:
        return self._list_files(self._memory_dir(scope, "note"))

    def _list_files(self, directory: Path) -> list[MemoryFile]:
        if not directory.is_dir():
            return []
        results: list[MemoryFile] = []
        for path in sorted(directory.glob("*.md")):
            mf = self._read_path(path)
            if mf is not None:
                results.append(mf)
        return results

    # ── Search ───────────────────────────────────────────────────

    @property
    def has_index(self) -> bool:
        return self._index is not None

    def search_notes(self, scope: str, query: str) -> list[MemoryFile]:
        """Search notes using the FTS5 index.

        Returns empty when no index is configured. Callers should check
        ``has_index`` to distinguish "no results" from "no index".
        """
        if self._index:
            return self._search_notes_indexed(scope, query)
        logger.warning("search_notes called without index; returning empty")
        return []

    def _search_notes_indexed(self, scope: str, query: str) -> list[MemoryFile]:
        assert self._index is not None
        results = self._index.search(query, scopes=[scope], kind="note")
        memory_files: list[MemoryFile] = []
        for r in results:
            path = self.base_dir / r.relative_path
            mf = self._read_path(path)
            if mf is not None:
                memory_files.append(mf)
        return memory_files

    def get_rule(self, scope: str, key: str) -> MemoryFile | None:
        return self._read_path(self._memory_path(scope, "rule", key))

    def get_note(self, scope: str, key: str) -> MemoryFile | None:
        return self._read_path(self._memory_path(scope, "note", key))

    def _forget_path(self, path: Path) -> bool:
        """Move a memory file to trash (not hard delete)."""
        if not path.is_file():
            return False

        relative = str(path.relative_to(self.base_dir))
        trash_dir = path.parent.parent / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)

        dest = _unique_path(trash_dir, path.stem)
        path.rename(dest)
        logger.info("moved to trash: %s → %s", path, dest)

        if self._index:
            self._index.delete(relative)

        return True

    def forget_rule(self, scope: str, key: str) -> bool:
        return self._forget_path(self._memory_path(scope, "rule", key))

    def forget_note(self, scope: str, key: str) -> bool:
        return self._forget_path(self._memory_path(scope, "note", key))

    # ── Helpers ────────────────────────────────────────────────────

    def memory_roots(self) -> list[Path]:
        """Return the root directories that contain memory files."""
        return [
            self.base_dir / "memory" / "global",
            self.base_dir / "memory" / "users",
            self.base_dir / "agents",
        ]

    # ── Sweep expired ────────────────────────────────────────────

    _SKIP_DIRS: ClassVar[set[str]] = {"node_modules", ".git", "__pycache__", "workspace"}

    def sweep_expired(self) -> int:
        """Move expired memory files to trash. Returns count of swept files."""
        now = _now_utc()
        count = 0

        for root in self.memory_roots():
            if not root.is_dir():
                continue
            for md_path in root.rglob("*.md"):
                if self._SKIP_DIRS & set(md_path.parts):
                    continue
                if md_path.parent.name not in ("rules", "notes"):
                    continue
                mf = _parse_memory_file(md_path, self.base_dir)
                if mf is None:
                    continue
                if _is_expired(mf.expires_at, now=now) and self._forget_path(md_path):
                    count += 1

        if self._index:
            self._index.delete_expired()

        if count:
            logger.info("swept %d expired memory files", count)
        return count
