from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from operator_ai.config import OPERATOR_DIR

logger = logging.getLogger("operator.memory")

_TTL_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)


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


def _slugify(content: str, max_len: int = 60) -> str:
    """Generate a slug from the first ~max_len chars of content."""
    text = content[:max_len].lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip("-")
    return text or "untitled"


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


def _write_memory_file(
    path: Path,
    content: str,
    *,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Write a memory file with YAML frontmatter."""
    now = _now_utc()
    created = created_at or now
    updated = updated_at or now

    frontmatter: dict[str, Any] = {
        "created_at": _format_dt(created),
        "updated_at": _format_dt(updated),
    }
    if expires_at is not None:
        frontmatter["expires_at"] = _format_dt(expires_at)

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.append(yaml.dump(frontmatter, default_flow_style=False).rstrip())
    lines.append("---")
    lines.append("")
    lines.append(content.rstrip())
    lines.append("")
    path.write_text("\n".join(lines))


def _parse_memory_file(path: Path, relative_to: Path) -> MemoryFile | None:
    """Parse a memory file, returning None on read errors."""
    try:
        text = path.read_text()
    except OSError:
        logger.warning("could not read memory file: %s", path)
        return None

    relative_path = str(path.relative_to(relative_to))
    created_at = None
    updated_at = None
    expires_at = None
    content = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter_text = parts[1]
            content = parts[2].strip()
            try:
                fm = yaml.safe_load(frontmatter_text) or {}
            except yaml.YAMLError:
                fm = {}
            if isinstance(fm, dict):
                created_at = _parse_dt(fm.get("created_at"))
                updated_at = _parse_dt(fm.get("updated_at"))
                expires_at = _parse_dt(fm.get("expires_at"))

    return MemoryFile(
        path=path,
        relative_path=relative_path,
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
        name = scope[6:]
        return base_dir / "agents" / name / "memory"
    if scope.startswith("user:"):
        name = scope[5:]
        return base_dir / "memory" / "users" / name
    raise ValueError(f"Invalid scope: {scope!r}")


def _has_ripgrep() -> bool:
    """Check if ripgrep (rg) is available on PATH."""
    return shutil.which("rg") is not None


@dataclass
class MemoryFile:
    path: Path
    relative_path: str
    created_at: datetime | None
    updated_at: datetime | None
    expires_at: datetime | None
    content: str


class MemoryStore:
    """File-backed memory store. No embeddings, no SQLite, no vectors."""

    def __init__(self, base_dir: Path = OPERATOR_DIR):
        self._base_dir = base_dir

    def _scope_dir(self, scope: str) -> Path:
        return _resolve_scope(scope, self._base_dir)

    def _relative_to(self) -> Path:
        return self._base_dir

    # ── Create ───────────────────────────────────────────────────

    def create_rule(self, scope: str, content: str, ttl: str | None = None) -> str:
        """Write a rule file and return its relative path."""
        scope_dir = self._scope_dir(scope)
        rules_dir = scope_dir / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)

        slug = _slugify(content)
        path = _unique_path(rules_dir, slug)

        expires_at = None
        if ttl:
            expires_at = _now_utc() + parse_ttl(ttl)

        _write_memory_file(path, content, expires_at=expires_at)
        relative = str(path.relative_to(self._relative_to()))
        logger.info("created rule: %s", relative)
        return relative

    def create_note(self, scope: str, content: str, ttl: str | None = None) -> str:
        """Write a note file and return its relative path."""
        scope_dir = self._scope_dir(scope)
        notes_dir = scope_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        slug = _slugify(content)
        path = _unique_path(notes_dir, slug)

        expires_at = None
        if ttl:
            expires_at = _now_utc() + parse_ttl(ttl)

        _write_memory_file(path, content, expires_at=expires_at)
        relative = str(path.relative_to(self._relative_to()))
        logger.info("created note: %s", relative)
        return relative

    # ── List ─────────────────────────────────────────────────────

    def list_rules(self, scope: str) -> list[MemoryFile]:
        """List all rule files in the given scope."""
        rules_dir = self._scope_dir(scope) / "rules"
        return self._list_files(rules_dir)

    def list_notes(self, scope: str) -> list[MemoryFile]:
        """List all note files in the given scope."""
        notes_dir = self._scope_dir(scope) / "notes"
        return self._list_files(notes_dir)

    def _list_files(self, directory: Path) -> list[MemoryFile]:
        if not directory.is_dir():
            return []
        results: list[MemoryFile] = []
        for path in sorted(directory.glob("*.md")):
            mf = _parse_memory_file(path, self._relative_to())
            if mf is not None:
                results.append(mf)
        return results

    # ── Search ───────────────────────────────────────────────────

    def search_notes(self, scope: str, query: str) -> list[MemoryFile]:
        """Search notes by filename and content substring.

        Uses ripgrep for content search when available, falls back to
        pathlib glob + read.
        """
        notes_dir = self._scope_dir(scope) / "notes"
        if not notes_dir.is_dir():
            return []

        query_lower = query.lower()

        # Filename matches (always via glob)
        filename_matches: list[MemoryFile] = []
        content_matches: list[MemoryFile] = []

        if _has_ripgrep():
            # Use ripgrep for content search
            rg_matches = self._rg_search(notes_dir, query)
            all_files = {mf.path: mf for mf in self._list_files(notes_dir)}

            for path in rg_matches:
                if path in all_files:
                    mf = all_files[path]
                    slug_part = path.stem.lower()
                    if query_lower in slug_part:
                        filename_matches.append(mf)
                    else:
                        content_matches.append(mf)

            # Also add filename matches that ripgrep may not have found
            for path, mf in all_files.items():
                slug_part = path.stem.lower()
                if query_lower in slug_part and mf not in filename_matches:
                    filename_matches.append(mf)
        else:
            # Fallback: pathlib glob + read
            for mf in self._list_files(notes_dir):
                slug_part = mf.path.stem.lower()
                if query_lower in slug_part:
                    filename_matches.append(mf)
                elif query_lower in mf.content.lower():
                    content_matches.append(mf)

        # Filename matches first, then content matches
        seen: set[Path] = set()
        results: list[MemoryFile] = []
        for mf in filename_matches + content_matches:
            if mf.path not in seen:
                seen.add(mf.path)
                results.append(mf)
        return results

    def _rg_search(self, directory: Path, query: str) -> list[Path]:
        """Run ripgrep and return matching file paths."""
        try:
            result = subprocess.run(
                ["rg", "--files-with-matches", "--ignore-case", "--no-messages", query],
                cwd=str(directory),
                capture_output=True,
                text=True,
                timeout=10,
            )
            paths: list[Path] = []
            for line in result.stdout.strip().splitlines():
                if line:
                    paths.append(directory / line)
            return paths
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

    # ── Read ─────────────────────────────────────────────────────

    def read(self, relative_path: str) -> MemoryFile | None:
        """Read a specific memory file by its relative path."""
        path = self._base_dir / relative_path
        if not path.is_file():
            return None
        return _parse_memory_file(path, self._relative_to())

    # ── Update ───────────────────────────────────────────────────

    def update(self, relative_path: str, content: str) -> bool:
        """Update a memory file's content and bump updated_at."""
        path = self._base_dir / relative_path
        if not path.is_file():
            return False

        existing = _parse_memory_file(path, self._relative_to())
        if existing is None:
            return False

        _write_memory_file(
            path,
            content,
            created_at=existing.created_at,
            updated_at=_now_utc(),
            expires_at=existing.expires_at,
        )
        logger.info("updated memory: %s", relative_path)
        return True

    # ── Forget ───────────────────────────────────────────────────

    def forget(self, relative_path: str) -> bool:
        """Move a memory file to trash (not hard delete)."""
        path = self._base_dir / relative_path
        if not path.is_file():
            return False

        # Determine the trash directory: it's a sibling of rules/ or notes/
        # e.g., agents/<name>/memory/notes/foo.md → agents/<name>/memory/trash/foo.md
        parent = path.parent
        trash_dir = parent.parent / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)

        dest = _unique_path(trash_dir, path.stem)
        path.rename(dest)
        logger.info("moved to trash: %s → %s", relative_path, dest.name)
        return True

    # ── Sweep expired ────────────────────────────────────────────

    def sweep_expired(self) -> int:
        """Move expired memory files to trash. Returns count of swept files."""
        now = _now_utc()
        count = 0

        # Walk all memory directories looking for .md files with expires_at
        memory_roots = [
            self._base_dir / "memory" / "global",
            self._base_dir / "memory" / "users",
            self._base_dir / "agents",
        ]

        for root in memory_roots:
            if not root.is_dir():
                continue
            for md_path in root.rglob("*.md"):
                # Only process files in rules/ or notes/ directories
                if md_path.parent.name not in ("rules", "notes"):
                    continue

                mf = _parse_memory_file(md_path, self._relative_to())
                if mf is None:
                    continue
                if mf.expires_at is not None and mf.expires_at <= now:
                    relative = str(md_path.relative_to(self._relative_to()))
                    if self.forget(relative):
                        count += 1

        if count:
            logger.info("swept %d expired memory files", count)
        return count
