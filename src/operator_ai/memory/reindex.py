"""Reindex memory files into the FTS5/vector index.

Shared between startup (main.py) and the CLI (operator memory index).
"""

from __future__ import annotations

import logging

from operator_ai.memory.index import MemoryIndex, content_hash, derive_scope_kind
from operator_ai.memory.store import MemoryFile, MemoryStore, _parse_memory_file

logger = logging.getLogger("operator.memory.reindex")


def reindex_diff(store: MemoryStore, index: MemoryIndex) -> tuple[int, int]:
    """Reindex only changed/new/deleted files. Returns (upserted, deleted)."""
    indexed_hashes = index.get_content_hashes()
    disk_files = _scan_disk_files(store)

    upserted = 0
    for rel_path, (mf, disk_hash) in disk_files.items():
        if indexed_hashes.get(rel_path) != disk_hash:
            scope, kind = derive_scope_kind(rel_path)
            index.upsert(
                rel_path,
                scope,
                kind,
                mf.key,
                mf.content,
                disk_hash,
                created_at=mf.created_at.timestamp() if mf.created_at else None,
                updated_at=mf.updated_at.timestamp() if mf.updated_at else None,
                expires_at=mf.expires_at.timestamp() if mf.expires_at else None,
            )
            upserted += 1

    deleted_paths = set(indexed_hashes.keys()) - set(disk_files.keys())
    if deleted_paths:
        index.delete_missing(deleted_paths)

    deleted = len(deleted_paths)
    logger.info("reindex (diff): %d upserted, %d deleted", upserted, deleted)
    return upserted, deleted


def reindex_full(store: MemoryStore, index: MemoryIndex) -> int:
    """Full rebuild: drop index and re-add all files. Returns count."""
    index.rebuild()
    disk_files = _scan_disk_files(store)

    count = 0
    for rel_path, (mf, disk_hash) in disk_files.items():
        scope, kind = derive_scope_kind(rel_path)
        index.upsert(
            rel_path,
            scope,
            kind,
            mf.key,
            mf.content,
            disk_hash,
            created_at=mf.created_at.timestamp() if mf.created_at else None,
            updated_at=mf.updated_at.timestamp() if mf.updated_at else None,
            expires_at=mf.expires_at.timestamp() if mf.expires_at else None,
        )
        count += 1

    logger.info("reindex (full): %d files indexed", count)
    return count


def _scan_disk_files(store: MemoryStore) -> dict[str, tuple[MemoryFile, str]]:
    """Walk all memory directories and return {relative_path: (MemoryFile, hash)}."""
    disk_files: dict[str, tuple[MemoryFile, str]] = {}

    for root in store.memory_roots():
        if not root.is_dir():
            continue
        for md_path in root.rglob("*.md"):
            if md_path.parent.name not in ("rules", "notes"):
                continue
            mf = _parse_memory_file(md_path, store.base_dir)
            if mf is None:
                continue
            disk_files[mf.relative_path] = (mf, content_hash(mf.content))

    return disk_files
