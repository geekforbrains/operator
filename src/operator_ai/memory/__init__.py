"""File-backed memory system with FTS5 search and optional vector embeddings."""

from operator_ai.memory.index import (
    IndexResult,
    MemoryIndex,
    build_fts_query,
    content_hash,
    derive_scope_kind,
)
from operator_ai.memory.reindex import reindex_diff, reindex_full
from operator_ai.memory.store import MemoryFile, MemoryStore, parse_ttl, write_memory_file

__all__ = [
    "IndexResult",
    "MemoryFile",
    "MemoryIndex",
    "MemoryStore",
    "build_fts_query",
    "content_hash",
    "derive_scope_kind",
    "parse_ttl",
    "reindex_diff",
    "reindex_full",
    "write_memory_file",
]
