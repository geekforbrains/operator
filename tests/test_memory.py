from __future__ import annotations

import asyncio

import pytest

from operator_ai.main import _conversation_memory_scopes
from operator_ai.memory import _parse_harvested_line
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
        {"id": 2, "content": "agent note", "scope": "agent", "pinned": 0}
    ]
    fake_memory_store.scoped_lists[("global", "global")] = [
        {"id": 1, "content": "global note", "scope": "global", "pinned": 0}
    ]
    fake_memory_store.scoped_lists[("user", "slack:U123")] = [
        {"id": 3, "content": "private note", "scope": "user", "pinned": 0}
    ]

    result = asyncio.run(memory_tools.list_memories())

    assert "[global] global note" in result
    assert "[agent] agent note" in result
    assert "private note" not in result


# -- harvester parse-level scope enforcement --------------------------------


def test_parse_harvested_line_rejects_user_scope_when_not_private() -> None:
    parsed = _parse_harvested_line(
        "- [user] Gavin likes espresso",
        user_id="slack:U123",
        agent_name="operator",
        allow_user_scope=False,
    )
    assert parsed is None


def test_parse_harvested_line_accepts_agent_and_global_when_not_private() -> None:
    parsed_agent = _parse_harvested_line(
        "- [agent] Project uses uv",
        user_id="",
        agent_name="operator",
        allow_user_scope=False,
    )
    parsed_global = _parse_harvested_line(
        "- [global] Python 3.11 is required",
        user_id="",
        agent_name="operator",
        allow_user_scope=False,
    )
    assert parsed_agent == ("agent", "operator", "Project uses uv")
    assert parsed_global == ("global", "global", "Python 3.11 is required")


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
    # Empty user_id should be excluded even in private context
    assert scopes == [("agent", "hermy"), ("global", "global")]


def test_conversation_memory_scopes_public_channel() -> None:
    scopes = _conversation_memory_scopes(
        user_id="gavin",
        agent_name="hermy",
        is_private=False,
    )
    # Public channels should not include user scope
    assert scopes == [("agent", "hermy"), ("global", "global")]
