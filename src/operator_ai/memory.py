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
_MEMORY_DIR_BY_KIND = {"rule": "rules", "note": "notes"}


def _word_variants(word: str) -> list[str]:
    """Generate common English suffix variants for fuzzy matching.

    Given a word, return the original plus forms with suffixes stripped or
    added so that "dogs" matches "dog", "running" matches "run", etc.
    """
    forms: list[str] = [word]
    # Strip suffixes: dogs→dog, watches→watch, traveled→travel, running→run
    if word.endswith("ies") and len(word) > 4:
        forms.append(word[:-3] + "y")  # families→family
    if word.endswith("ses") and len(word) > 4:
        forms.append(word[:-2])  # buses→bus
    if word.endswith("es") and len(word) > 4:
        forms.append(word[:-2])  # watches→watch
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        forms.append(word[:-1])  # dogs→dog
    if word.endswith("ed") and len(word) > 4:
        forms.append(word[:-2])  # traveled→travel
        forms.append(word[:-1])  # named→name (strip d only)
    if word.endswith("ing") and len(word) > 5:
        forms.append(word[:-3])  # running→runn
        forms.append(word[:-3] + "e")  # hiking→hike
    # Add plural: dog→dogs
    if not word.endswith("s"):
        forms.append(word + "s")
    # Dedupe while preserving order
    return list(dict.fromkeys(f for f in forms if len(f) >= 2))


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
        name = scope[6:]
        return base_dir / "agents" / name / "memory"
    if scope.startswith("user:"):
        name = scope[5:]
        return base_dir / "memory" / "users" / name
    raise ValueError(f"Invalid scope: {scope!r}")


def _has_ripgrep() -> bool:
    """Check if ripgrep (rg) is available on PATH."""
    return shutil.which("rg") is not None


def _normalize_key(key: str, max_len: int = 80) -> str:
    """Normalize a memory key into a stable filename-safe slug."""
    if not re.search(r"[a-z0-9]", key, re.IGNORECASE):
        raise ValueError(f"Invalid memory key: {key!r}")
    normalized = _slugify(key, max_len=max_len)
    return normalized


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
    """File-backed memory store. No embeddings, no SQLite, no vectors."""

    def __init__(self, base_dir: Path = OPERATOR_DIR):
        self._base_dir = base_dir

    def _scope_dir(self, scope: str) -> Path:
        return _resolve_scope(scope, self._base_dir)

    def _relative_to(self) -> Path:
        return self._base_dir

    def _memory_dir(self, scope: str, kind: str) -> Path:
        try:
            subdir = _MEMORY_DIR_BY_KIND[kind]
        except KeyError:
            raise ValueError(f"Invalid memory kind: {kind!r}") from None
        return self._scope_dir(scope) / subdir

    def _memory_path(self, scope: str, kind: str, key: str) -> Path:
        normalized_key = _normalize_key(key)
        return self._memory_dir(scope, kind) / f"{normalized_key}.md"

    def _read_path(self, path: Path, *, include_expired: bool = False) -> MemoryFile | None:
        if not path.is_file():
            return None
        mf = _parse_memory_file(path, self._relative_to())
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
        expires_at = _now_utc() + parse_ttl(ttl) if ttl else None
        _write_memory_file(
            path,
            content,
            created_at=existing.created_at if existing else None,
            updated_at=_now_utc(),
            expires_at=expires_at,
        )
        relative = str(path.relative_to(self._relative_to()))
        logger.info("saved %s: %s", kind, relative)
        return relative

    def upsert_rule(self, scope: str, key: str, content: str) -> str:
        """Create or replace a rule file and return its relative path."""
        return self._upsert(scope, "rule", key, content)

    def upsert_note(self, scope: str, key: str, content: str, ttl: str | None = None) -> str:
        """Create or replace a note file and return its relative path."""
        return self._upsert(scope, "note", key, content, ttl=ttl)

    # ── List ─────────────────────────────────────────────────────

    def list_rules(self, scope: str) -> list[MemoryFile]:
        """List all rule files in the given scope."""
        return self._list_files(self._memory_dir(scope, "rule"))

    def list_notes(self, scope: str) -> list[MemoryFile]:
        """List all note files in the given scope."""
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

    def search_notes(self, scope: str, query: str) -> list[MemoryFile]:
        """Search notes by filename and content, matching any word variant.

        The query is split into words and each word is expanded with common
        English suffix variants (dogs→dog, running→run, etc.).  A note
        matches if any variant appears in its filename or content.
        """
        notes_dir = self._memory_dir(scope, "note")
        if not notes_dir.is_dir():
            return []

        words = [w for w in query.lower().split() if w]
        if not words:
            return []

        # Expand each query word into variant forms
        variants: list[str] = []
        for w in words:
            variants.extend(_word_variants(w))
        variants = list(dict.fromkeys(variants))  # dedupe, preserve order

        all_files = {mf.path: mf for mf in self._list_files(notes_dir)}
        if not all_files:
            return []

        # Collect content matches via ripgrep or fallback
        content_hit_paths: set[Path] = set()
        if _has_ripgrep():
            content_hit_paths = set(self._rg_search(notes_dir, "|".join(variants)))
        else:
            for path, mf in all_files.items():
                content_lower = mf.content.lower()
                if any(v in content_lower for v in variants):
                    content_hit_paths.add(path)

        # Score each note: count how many variants appear in filename + content
        scored: list[tuple[int, bool, MemoryFile]] = []
        for path, mf in all_files.items():
            slug = path.stem.lower()
            content_lower = mf.content.lower()
            filename_hits = sum(1 for v in variants if v in slug)
            content_hits = (
                sum(1 for v in variants if v in content_lower) if path in content_hit_paths else 0
            )
            total = filename_hits + content_hits
            if total > 0:
                scored.append((total, filename_hits > 0, mf))

        # Sort: most hits first, filename matches break ties
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [mf for _, _, mf in scored]

    def _rg_search(self, directory: Path, pattern: str) -> list[Path]:
        """Run ripgrep with a regex pattern and return matching file paths."""
        try:
            result = subprocess.run(
                ["rg", "--files-with-matches", "--ignore-case", "--no-messages", pattern],
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

    def get_rule(self, scope: str, key: str) -> MemoryFile | None:
        """Read a specific active rule by deterministic key."""
        return self._read_path(self._memory_path(scope, "rule", key))

    def get_note(self, scope: str, key: str) -> MemoryFile | None:
        """Read a specific active note by deterministic key."""
        return self._read_path(self._memory_path(scope, "note", key))

    def _forget_path(self, path: Path) -> bool:
        """Move a memory file to trash (not hard delete)."""
        if not path.is_file():
            return False

        parent = path.parent
        trash_dir = parent.parent / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)

        dest = _unique_path(trash_dir, path.stem)
        path.rename(dest)
        logger.info("moved to trash: %s → %s", path, dest)
        return True

    def forget_rule(self, scope: str, key: str) -> bool:
        """Move a rule file to trash by deterministic key."""
        return self._forget_path(self._memory_path(scope, "rule", key))

    def forget_note(self, scope: str, key: str) -> bool:
        """Move a note file to trash by deterministic key."""
        return self._forget_path(self._memory_path(scope, "note", key))

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
                if _is_expired(mf.expires_at, now=now) and self._forget_path(md_path):
                    count += 1

        if count:
            logger.info("swept %d expired memory files", count)
        return count
