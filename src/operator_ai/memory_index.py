"""FTS5-backed search index for memory files.

The index is derived from markdown files on disk and is fully rebuildable.
Files remain the source of truth. The index provides fast, ranked search
via SQLite FTS5 with Porter stemming, and optional vector search via
sqlite-vec when an embedding model is configured.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("operator.memory.index")


@dataclass
class IndexResult:
    """A single search result from the memory index."""

    relative_path: str
    scope: str
    kind: str
    key: str
    rank: float


@dataclass
class EmbeddingConfig:
    """Configuration for optional vector embeddings."""

    model: str
    dimensions: int = 1536


def _content_hash(text: str) -> str:
    """MD5 hash of content for change detection (not security)."""
    return hashlib.md5(text.encode()).hexdigest()


def _derive_scope_kind(relative_path: str) -> tuple[str, str]:
    """Parse a relative memory path into (scope, kind).

    Examples:
        memory/global/notes/foo.md       -> ("global", "note")
        memory/global/rules/bar.md       -> ("global", "rule")
        agents/operator/memory/notes/x.md -> ("agent:operator", "note")
        memory/users/gavin/rules/y.md    -> ("user:gavin", "rule")
    """
    parts = Path(relative_path).parts

    if len(parts) >= 4 and parts[0] == "memory" and parts[1] == "global":
        kind_dir = parts[2]  # "rules" or "notes"
        kind = "rule" if kind_dir == "rules" else "note"
        return ("global", kind)

    if len(parts) >= 5 and parts[0] == "memory" and parts[1] == "users":
        username = parts[2]
        kind_dir = parts[3]
        kind = "rule" if kind_dir == "rules" else "note"
        return (f"user:{username}", kind)

    if len(parts) >= 5 and parts[0] == "agents" and parts[2] == "memory":
        agent_name = parts[1]
        kind_dir = parts[3]
        kind = "rule" if kind_dir == "rules" else "note"
        return (f"agent:{agent_name}", kind)

    raise ValueError(f"Cannot derive scope/kind from path: {relative_path}")


class MemoryIndex:
    """SQLite FTS5 search index derived from memory files.

    Provides Porter-stemmed full-text search with BM25 ranking. Optionally
    supports vector similarity search when an embedding model is configured
    and sqlite-vec is installed.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        embed_fn: Callable[[str], list[float]] | None = None,
        embedding_dimensions: int = 1536,
    ):
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._embedding_dimensions = embedding_dimensions
        self._has_vec = False

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        logger.info("memory index: initialized at %s", db_path)

    def _init_db(self) -> None:
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        # Metadata table — one row per memory file
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_meta (
                relative_path TEXT PRIMARY KEY,
                scope         TEXT NOT NULL,
                kind          TEXT NOT NULL,
                key           TEXT NOT NULL,
                content       TEXT NOT NULL DEFAULT '',
                content_hash  TEXT NOT NULL,
                created_at    REAL,
                updated_at    REAL,
                expires_at    REAL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_meta_scope_kind
            ON memory_meta(scope, kind)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_meta_expires
            ON memory_meta(expires_at) WHERE expires_at IS NOT NULL
        """)

        # FTS5 virtual table for full-text search with Porter stemming
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                relative_path,
                key,
                content,
                tokenize='porter unicode61'
            )
        """)

        # Optional: vector table via sqlite-vec
        if self._embed_fn is not None:
            self._init_vec()

        self._conn.commit()

    def _init_vec(self) -> None:
        """Try to load sqlite-vec and create the vector table."""
        try:
            import sqlite_vec  # type: ignore[import-untyped]

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                    relative_path TEXT PRIMARY KEY,
                    embedding FLOAT[{self._embedding_dimensions}]
                )
            """)
            self._has_vec = True
            logger.info(
                "memory index: sqlite-vec loaded, vector search enabled (%dd)",
                self._embedding_dimensions,
            )
        except ImportError:
            logger.warning("memory index: sqlite-vec not installed; vector search disabled")
            self._has_vec = False
        except Exception:
            logger.warning("memory index: failed to load sqlite-vec", exc_info=True)
            self._has_vec = False

    # ── Write ────────────────────────────────────────────────────

    def upsert(
        self,
        relative_path: str,
        scope: str,
        kind: str,
        key: str,
        content: str,
        content_hash: str,
        *,
        created_at: float | None = None,
        updated_at: float | None = None,
        expires_at: float | None = None,
    ) -> None:
        """Index a memory file. Replaces any existing entry for the path."""
        # Update metadata
        self._conn.execute(
            """
            INSERT INTO memory_meta
                (relative_path, scope, kind, key, content, content_hash,
                 created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                scope=excluded.scope, kind=excluded.kind, key=excluded.key,
                content=excluded.content, content_hash=excluded.content_hash,
                created_at=excluded.created_at, updated_at=excluded.updated_at,
                expires_at=excluded.expires_at
            """,
            (
                relative_path,
                scope,
                kind,
                key,
                content,
                content_hash,
                created_at,
                updated_at,
                expires_at,
            ),
        )

        # Update FTS: delete old, insert new
        self._conn.execute(
            "DELETE FROM memory_fts WHERE relative_path = ?",
            (relative_path,),
        )
        self._conn.execute(
            "INSERT INTO memory_fts (relative_path, key, content) VALUES (?, ?, ?)",
            (relative_path, key, content),
        )

        self._conn.commit()
        logger.debug("indexed: %s", relative_path)

    def embed(self, relative_path: str, content: str) -> None:
        """Compute and store the vector embedding for a memory file.

        Call this after upsert when embeddings are configured. Safe to call
        even if sqlite-vec is not available (no-op).
        """
        if not self._has_vec or not self._embed_fn:
            return

        try:
            embedding = self._embed_fn(content)
            # Delete old vector if exists
            self._conn.execute(
                "DELETE FROM memory_vec WHERE relative_path = ?",
                (relative_path,),
            )
            self._conn.execute(
                "INSERT INTO memory_vec (relative_path, embedding) VALUES (?, ?)",
                (relative_path, _serialize_vec(embedding)),
            )
            self._conn.commit()
            logger.debug("embedded: %s", relative_path)
        except Exception:
            logger.warning("embedding failed for %s", relative_path, exc_info=True)

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, relative_path: str) -> None:
        """Remove a memory file from the index."""
        self._conn.execute(
            "DELETE FROM memory_meta WHERE relative_path = ?",
            (relative_path,),
        )
        self._conn.execute(
            "DELETE FROM memory_fts WHERE relative_path = ?",
            (relative_path,),
        )
        if self._has_vec:
            self._conn.execute(
                "DELETE FROM memory_vec WHERE relative_path = ?",
                (relative_path,),
            )
        self._conn.commit()
        logger.debug("removed from index: %s", relative_path)

    # ── Search ───────────────────────────────────────────────────

    def search(
        self,
        query: str,
        scopes: list[str],
        kind: str = "note",
        limit: int = 20,
    ) -> list[IndexResult]:
        """Search memory using FTS5 with optional vector re-ranking.

        Returns results ranked by BM25 relevance.
        """
        query = query.strip()
        if not query:
            return []

        fts_query = _build_fts_query(query)
        if not fts_query:
            return []

        placeholders = ",".join("?" for _ in scopes)
        params: list[str | int] = [fts_query, *scopes, kind, limit]

        rows = self._conn.execute(
            f"""
            SELECT m.relative_path, m.scope, m.kind, m.key, fts.rank
            FROM memory_fts fts
            JOIN memory_meta m ON m.relative_path = fts.relative_path
            WHERE memory_fts MATCH ?
              AND m.scope IN ({placeholders})
              AND m.kind = ?
              AND (m.expires_at IS NULL OR m.expires_at > unixepoch('now'))
            ORDER BY fts.rank
            LIMIT ?
            """,
            params,
        ).fetchall()

        results = [
            IndexResult(
                relative_path=r["relative_path"],
                scope=r["scope"],
                kind=r["kind"],
                key=r["key"],
                rank=r["rank"],
            )
            for r in rows
        ]

        logger.debug(
            "FTS5 search %r in scopes=%s kind=%s: %d results",
            query,
            scopes,
            kind,
            len(results),
        )

        # If vector search is available, fuse results
        if self._has_vec and self._embed_fn and results:
            results = self._fuse_with_vectors(query, scopes, kind, results, limit)

        return results

    def _fuse_with_vectors(
        self,
        query: str,
        scopes: list[str],
        kind: str,
        fts_results: list[IndexResult],
        limit: int,
    ) -> list[IndexResult]:
        """Fuse FTS5 and vector search results using Reciprocal Rank Fusion."""
        try:
            query_vec = self._embed_fn(query)  # type: ignore[misc]
        except Exception:
            logger.warning("vector query embedding failed, using FTS5 only", exc_info=True)
            return fts_results

        placeholders = ",".join("?" for _ in scopes)
        vec_rows = self._conn.execute(
            f"""
            SELECT v.relative_path, v.distance
            FROM memory_vec v
            JOIN memory_meta m ON m.relative_path = v.relative_path
            WHERE v.embedding MATCH ?
              AND m.scope IN ({placeholders})
              AND m.kind = ?
              AND (m.expires_at IS NULL OR m.expires_at > unixepoch('now'))
            ORDER BY v.distance
            LIMIT ?
            """,
            (_serialize_vec(query_vec), *scopes, kind, limit),
        ).fetchall()

        # RRF fusion: score = sum(1 / (k + rank)) across both lists
        k = 60  # standard RRF constant
        rrf_scores: dict[str, float] = {}

        for rank, r in enumerate(fts_results):
            rrf_scores[r.relative_path] = rrf_scores.get(r.relative_path, 0) + 1.0 / (k + rank)

        for rank, row in enumerate(vec_rows):
            path = row["relative_path"]
            rrf_scores[path] = rrf_scores.get(path, 0) + 1.0 / (k + rank)

        # Build lookup for metadata
        meta_by_path = {r.relative_path: r for r in fts_results}
        for row in vec_rows:
            path = row["relative_path"]
            if path not in meta_by_path:
                # Vector found something FTS missed — look up metadata
                meta_row = self._conn.execute(
                    "SELECT scope, kind, key FROM memory_meta WHERE relative_path = ?",
                    (path,),
                ).fetchone()
                if meta_row:
                    meta_by_path[path] = IndexResult(
                        relative_path=path,
                        scope=meta_row["scope"],
                        kind=meta_row["kind"],
                        key=meta_row["key"],
                        rank=0.0,
                    )

        # Sort by RRF score descending, build results
        sorted_paths = sorted(rrf_scores, key=lambda p: rrf_scores[p], reverse=True)
        fused = []
        for path in sorted_paths[:limit]:
            if path in meta_by_path:
                r = meta_by_path[path]
                fused.append(
                    IndexResult(
                        relative_path=r.relative_path,
                        scope=r.scope,
                        kind=r.kind,
                        key=r.key,
                        rank=rrf_scores[path],
                    )
                )

        logger.debug(
            "RRF fusion: %d FTS + %d vec -> %d fused results",
            len(fts_results),
            len(vec_rows),
            len(fused),
        )
        return fused

    # ── Reindex ──────────────────────────────────────────────────

    def get_content_hashes(self) -> dict[str, str]:
        """Return {relative_path: content_hash} for all indexed files."""
        rows = self._conn.execute("SELECT relative_path, content_hash FROM memory_meta").fetchall()
        return {r["relative_path"]: r["content_hash"] for r in rows}

    def delete_missing(self, paths: set[str]) -> None:
        """Remove index entries for files that no longer exist on disk."""
        for path in paths:
            self.delete(path)
        if paths:
            logger.info("removed %d stale entries from index", len(paths))

    def rebuild(self) -> None:
        """Drop and recreate all index tables. Use with --force."""
        logger.info("rebuilding index from scratch")
        self._conn.execute("DELETE FROM memory_fts")
        self._conn.execute("DELETE FROM memory_meta")
        if self._has_vec:
            self._conn.execute("DELETE FROM memory_vec")
        self._conn.commit()

    # ── Sweep ────────────────────────────────────────────────────

    def delete_expired(self) -> int:
        """Remove index entries whose expires_at is in the past. Returns count."""
        # Collect paths first to also clean FTS and vec
        rows = self._conn.execute(
            """
            SELECT relative_path FROM memory_meta
            WHERE expires_at IS NOT NULL AND expires_at <= unixepoch('now')
            """
        ).fetchall()

        count = 0
        for row in rows:
            self.delete(row["relative_path"])
            count += 1

        if count:
            logger.info("swept %d expired entries from index", count)
        return count

    # ── Stats ────────────────────────────────────────────────────

    def count(self) -> int:
        """Return the total number of indexed memory files."""
        row = self._conn.execute("SELECT COUNT(*) as n FROM memory_meta").fetchone()
        return row["n"] if row else 0

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()
        logger.debug("memory index: closed")


# ── Helpers ──────────────────────────────────────────────────────

_FTS_UNSAFE = re.compile(r"[^\w\s]", re.UNICODE)


def _build_fts_query(raw: str) -> str:
    """Build an FTS5 query from a raw user string.

    Strips special characters and joins words with implicit AND.
    Each word gets a prefix match via *.
    """
    words = _FTS_UNSAFE.sub(" ", raw).split()
    if not words:
        return ""
    # Each word as a prefix match, joined with AND
    return " ".join(f'"{w}"*' for w in words)


def _serialize_vec(vec: list[float]) -> bytes:
    """Serialize a float vector to bytes for sqlite-vec."""
    import struct

    return struct.pack(f"{len(vec)}f", *vec)
