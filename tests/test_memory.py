from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from operator_ai.config import CleanerConfig, HarvesterConfig
from operator_ai.main import _conversation_memory_scopes
from operator_ai.memory import MemoryCleaner, MemoryHarvester, _parse_harvested_memories
from operator_ai.tools import memory as memory_tools


@pytest.fixture(autouse=True)
def _configure_public_context(fake_memory_store) -> None:
    memory_tools.configure(
        {
            "memory_store": fake_memory_store,
            "user_id": "slack:U123",
            "agent_name": "operator",
            "allow_user_scope": False,
        }
    )


# -- tool-level scope enforcement ------------------------------------------


def test_save_memory_blocks_user_scope_in_public_context(fake_memory_store) -> None:
    result = asyncio.run(memory_tools.save_memory("secret", scope="user"))

    assert "only allowed in private conversations" in result
    assert fake_memory_store.saved == []


def test_save_memory_accepts_retention(fake_memory_store) -> None:
    result = asyncio.run(
        memory_tools.save_memory("release note", scope="agent", retention="candidate")
    )

    assert "retention=candidate" in result
    assert fake_memory_store.saved == [("release note", "agent", "operator", False, "candidate")]


def test_search_memories_default_scope_excludes_user_in_public_context(
    fake_memory_store,
) -> None:
    asyncio.run(memory_tools.search_memories("deploy status"))

    assert fake_memory_store.search_calls
    assert fake_memory_store.search_calls[0]["scopes"] == [
        ("agent", "operator"),
        ("global", "global"),
    ]


def test_list_memories_in_public_context_filters_to_agent_and_global(
    fake_memory_store,
) -> None:
    fake_memory_store.scoped_lists[("agent", "operator")] = [
        {
            "id": 2,
            "content": "agent note",
            "scope": "agent",
            "retention": "durable",
            "pinned": 0,
            "expires_at": None,
        }
    ]
    fake_memory_store.scoped_lists[("global", "global")] = [
        {
            "id": 1,
            "content": "global note",
            "scope": "global",
            "retention": "durable",
            "pinned": 0,
            "expires_at": None,
        }
    ]
    fake_memory_store.scoped_lists[("user", "slack:U123")] = [
        {
            "id": 3,
            "content": "private note",
            "scope": "user",
            "retention": "durable",
            "pinned": 0,
            "expires_at": None,
        }
    ]

    result = asyncio.run(memory_tools.list_memories())

    assert "[global] [durable] global note" in result
    assert "[agent] [durable] agent note" in result
    assert "private note" not in result


def test_list_memories_private_context_only_shows_current_scopes(fake_memory_store) -> None:
    memory_tools.configure(
        {
            "memory_store": fake_memory_store,
            "user_id": "gavin",
            "agent_name": "operator",
            "allow_user_scope": True,
        }
    )
    fake_memory_store.scoped_lists[("user", "gavin")] = [
        {
            "id": 3,
            "content": "current user note",
            "scope": "user",
            "retention": "candidate",
            "pinned": 0,
            "expires_at": "2026-03-20T00:00:00Z",
        }
    ]
    fake_memory_store.scoped_lists[("agent", "operator")] = [
        {
            "id": 2,
            "content": "agent note",
            "scope": "agent",
            "retention": "durable",
            "pinned": 0,
            "expires_at": None,
        }
    ]
    fake_memory_store.scoped_lists[("global", "global")] = [
        {
            "id": 1,
            "content": "global note",
            "scope": "global",
            "retention": "durable",
            "pinned": 1,
            "expires_at": None,
        }
    ]
    fake_memory_store.scoped_lists[("user", "other-user")] = [
        {
            "id": 4,
            "content": "other user note",
            "scope": "user",
            "retention": "durable",
            "pinned": 0,
            "expires_at": None,
        }
    ]
    fake_memory_store.list_all = [
        {
            "id": 99,
            "content": "leaked global dump",
            "scope": "global",
            "retention": "durable",
            "pinned": 0,
            "expires_at": None,
        }
    ]

    result = asyncio.run(memory_tools.list_memories())

    assert "current user note" in result
    assert "agent note" in result
    assert "global note" in result
    assert "other user note" not in result
    assert "leaked global dump" not in result


# -- harvester parse-level scope enforcement --------------------------------


def test_parse_harvested_memories_rejects_user_scope_when_not_private() -> None:
    parsed = _parse_harvested_memories(
        '[{"scope":"user","retention":"durable","content":"Gavin likes espresso"}]',
        user_id="slack:U123",
        agent_name="operator",
        allow_user_scope=False,
    )
    assert parsed == []


def test_parse_harvested_memories_accepts_retention_and_scopes() -> None:
    parsed = _parse_harvested_memories(
        """
        [
          {"scope":"agent","retention":"durable","content":"Project uses uv"},
          {"scope":"global","retention":"candidate","content":"Release checklist is active this week"}
        ]
        """,
        user_id="",
        agent_name="operator",
        allow_user_scope=False,
    )

    assert [(item.scope, item.scope_id, item.retention, item.content) for item in parsed] == [
        ("agent", "operator", "durable", "Project uses uv"),
        ("global", "global", "candidate", "Release checklist is active this week"),
    ]


def test_memory_harvester_omits_temperature(monkeypatch, fake_memory_store) -> None:
    captured: dict[str, object] = {}

    async def fake_completion(models, *, label, **kwargs):
        captured["models"] = models
        captured["label"] = label
        captured["kwargs"] = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="NONE"))])

    monkeypatch.setattr("operator_ai.memory._completion_with_fallback", fake_completion)

    harvester = MemoryHarvester(
        fake_memory_store,
        object(),
        HarvesterConfig(models=["openai/gpt-5.4"]),
    )

    extracted = asyncio.run(
        harvester._extract_memories(
            "user: hi\nassistant: hello",
            "gavin",
            "operator",
            allow_user_scope=False,
        )
    )

    assert extracted == 0
    assert captured["label"] == "harvester"
    assert "temperature" not in captured["kwargs"]


def test_memory_cleaner_omits_temperature(monkeypatch, fake_memory_store) -> None:
    captured: dict[str, object] = {}

    async def fake_completion(models, *, label, **kwargs):
        captured["models"] = models
        captured["label"] = label
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "keep": [
                                    {"id": 1, "content": "first"},
                                    {"id": 2, "content": "second"},
                                ],
                                "add": [],
                                "delete": [],
                            }
                        )
                    )
                )
            ]
        )

    class FakeStore:
        def get_all_memories_for_scope(self, _scope: str, _scope_id: str):
            return [
                {"id": 1, "content": "first", "retention": "durable", "pinned": 0},
                {"id": 2, "content": "second", "retention": "candidate", "pinned": 0},
            ]

        def update_memory(self, _mid: int, _new_content: str, _vec_bytes: bytes) -> None:
            raise AssertionError("update_memory should not be called")

        def delete_memory(self, _mid: int) -> None:
            raise AssertionError("delete_memory should not be called")

    monkeypatch.setattr("operator_ai.memory._completion_with_fallback", fake_completion)

    cleaner = MemoryCleaner(
        fake_memory_store,
        FakeStore(),
        CleanerConfig(models=["openai/gpt-5.4"]),
    )

    asyncio.run(cleaner._clean_scope("agent", "operator"))

    assert captured["label"] == "cleaner"
    assert "temperature" not in captured["kwargs"]


# -- memory scopes use username, not platform ID ----------------------------


def test_conversation_memory_scopes_uses_username() -> None:
    scopes = _conversation_memory_scopes(
        user_id="gavin",
        agent_name="hermy",
        is_private=True,
    )
    assert scopes == [("user", "gavin"), ("agent", "hermy"), ("global", "global")]


def test_conversation_memory_scopes_private_without_user() -> None:
    scopes = _conversation_memory_scopes(
        user_id="",
        agent_name="hermy",
        is_private=True,
    )
    assert scopes == [("agent", "hermy"), ("global", "global")]


def test_conversation_memory_scopes_public_channel() -> None:
    scopes = _conversation_memory_scopes(
        user_id="gavin",
        agent_name="hermy",
        is_private=False,
    )
    assert scopes == [("agent", "hermy"), ("global", "global")]
