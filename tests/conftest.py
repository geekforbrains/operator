from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FakeMemoryStore:
    search_results: list[dict[str, Any]] = field(default_factory=list)
    list_all: list[dict[str, Any]] = field(default_factory=list)
    scoped_lists: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    saved: list[tuple[str, str, str, bool, str]] = field(default_factory=list)
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    list_calls: list[dict[str, Any]] = field(default_factory=list)

    async def save(
        self,
        content: str,
        scope: str,
        scope_id: str,
        pinned: bool = False,
        retention: str = "durable",
    ) -> int | None:
        self.saved.append((content, scope, scope_id, pinned, retention))
        return len(self.saved)

    async def search(
        self,
        query: str,
        scopes: list[tuple[str, str]],
        top_k: int | None = None,
        min_relevance: float | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls.append(
            {
                "query": query,
                "scopes": scopes,
                "top_k": top_k,
                "min_relevance": min_relevance,
            }
        )
        return self.search_results

    def list_memories(
        self,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.list_calls.append(
            {
                "scope": scope,
                "scope_id": scope_id,
                "limit": limit,
                "offset": offset,
            }
        )
        if scope is None:
            rows = self.list_all
        else:
            rows = self.scoped_lists.get((scope, scope_id or ""), [])
        return rows[offset : offset + limit]


@pytest.fixture
def fake_memory_store() -> FakeMemoryStore:
    return FakeMemoryStore()
