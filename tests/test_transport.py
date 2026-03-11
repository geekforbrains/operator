from __future__ import annotations

import asyncio
import io
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import operator_ai.tools  # noqa: F401  — warm up circular imports
from operator_ai.transport.base import Attachment, IncomingMessage, MessageContext, Transport
from operator_ai.transport.cli import CliTransport
from operator_ai.transport.registry import (
    normalize_transport_options,
    transport_logger_names,
)
from operator_ai.transport.slack import SlackTransport, SlackUserProfile


def _make_transport(**kwargs: object) -> SlackTransport:
    return SlackTransport(
        name="slack",
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
        **kwargs,
    )


def _make_user_profile(
    user_id: str,
    display_name: str,
    *,
    is_bot: bool = False,
    is_deleted: bool = False,
) -> SlackUserProfile:
    return SlackUserProfile(
        user_id=user_id,
        slack_name=display_name.lower(),
        display_name=display_name,
        real_name=display_name,
        email=f"{display_name.lower()}@example.com",
        is_bot=is_bot,
        is_deleted=is_deleted,
    )


def _tool_func(transport: SlackTransport, name: str):
    for tool in transport.get_tools():
        if tool.name == name:
            return tool.func
    raise AssertionError(f"Tool not found: {name}")


def test_to_prompt_with_username() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
        username="gavin",
    )
    result = ctx.to_prompt()
    assert "- User: gavin (Gavin Vickery via slack)" in result


def test_to_prompt_without_username() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
    )
    result = ctx.to_prompt()
    assert "- User: Gavin Vickery (`U04ABC123`)" in result


def test_slack_message_formatting_uses_configured_timezone() -> None:
    transport = _make_transport(tz=ZoneInfo("Asia/Tokyo"))

    async def _resolve_user(_user_id: str) -> str:
        return "Gavin"

    transport._resolve_user = _resolve_user  # type: ignore[method-assign]

    formatted = asyncio.run(
        transport._format_messages(
            [
                {
                    "user": "U123",
                    "ts": "1704110400",  # 2024-01-01 12:00:00 UTC
                    "text": "Hello",
                }
            ]
        )
    )

    assert "[Gavin] 9:00 PM: Hello" in formatted


def test_slack_text_rendering_preserves_user_mentions() -> None:
    store = SimpleNamespace(
        resolve_username=lambda platform_id: {"slack:U234": "gavin"}.get(platform_id),
        list_users=lambda: [],
    )
    transport = _make_transport(store=store)

    async def _resolve_user(user_id: str) -> str:
        return {"UBOT": "Operator", "U234": "Gavin Vickery"}[user_id]

    transport._resolve_user = _resolve_user  # type: ignore[method-assign]

    rendered = asyncio.run(
        transport._render_slack_text(
            "<@UBOT> ask <@U234> about the deploy",
            strip_leading_mention=True,
        )
    )

    assert rendered == "ask <@U234> (Gavin Vickery) about the deploy"


def test_slack_message_formatting_renders_mentions() -> None:
    store = SimpleNamespace(
        resolve_username=lambda platform_id: {"slack:U234": "gavin"}.get(platform_id),
        list_users=lambda: [],
    )
    transport = _make_transport(store=store)

    async def _resolve_user(user_id: str) -> str:
        return {"U123": "Avery", "U234": "Gavin Vickery"}[user_id]

    transport._resolve_user = _resolve_user  # type: ignore[method-assign]

    formatted = asyncio.run(
        transport._format_messages(
            [
                {
                    "user": "U123",
                    "ts": "1704110400",
                    "text": "Ask <@U234> about the deploy",
                }
            ]
        )
    )

    assert "<@U234> (Gavin Vickery)" in formatted


def test_slack_text_rendering_expands_channel_mentions() -> None:
    transport = _make_transport()

    rendered = asyncio.run(
        transport._render_slack_text(
            "<@UBOT> whats new in <#C123|code>?", strip_leading_mention=True
        )
    )

    assert rendered == "whats new in <#C123> (#code)?"


def test_slack_start_bulk_loads_caches_before_handler_blocks(monkeypatch) -> None:
    state = {"users_loaded": False, "channels_loaded": False, "start_called": False}

    class FakeApp:
        def __init__(self, token: str):
            self.token = token
            self.client = SimpleNamespace()

        def event(self, _name: str):
            def _decorator(func):
                return func

            return _decorator

    class FakeHandler:
        def __init__(self, app: FakeApp, token: str):
            self.app = app
            self.token = token

        async def start_async(self) -> None:
            assert state["users_loaded"] is True
            assert state["channels_loaded"] is True
            state["start_called"] = True
            return None

        async def close_async(self) -> None:
            return None

    async def _noop(_msg) -> None:
        return None

    async def _run() -> None:
        transport = _make_transport()
        monkeypatch.setattr("operator_ai.transport.slack.AsyncApp", FakeApp)
        monkeypatch.setattr("operator_ai.transport.slack.AsyncSocketModeHandler", FakeHandler)

        async def _load_users() -> None:
            state["users_loaded"] = True

        async def _load_channels() -> None:
            state["channels_loaded"] = True

        transport._fetch_all_users = AsyncMock(side_effect=_load_users)  # type: ignore[method-assign]
        transport._fetch_all_channels = AsyncMock(side_effect=_load_channels)  # type: ignore[method-assign]
        await transport.start(_noop)
        await transport.stop()
        transport._fetch_all_users.assert_awaited_once()
        transport._fetch_all_channels.assert_awaited_once()

    asyncio.run(_run())

    assert state["start_called"] is True


def test_fetch_all_channels_ignores_archived_channels_by_default() -> None:
    async def _run() -> None:
        transport = _make_transport()
        client = SimpleNamespace(
            conversations_list=AsyncMock(
                return_value={
                    "channels": [
                        {
                            "id": "C1",
                            "name": "general",
                            "topic": {"value": "General chat"},
                        },
                        {
                            "id": "C2",
                            "name": "archived-room",
                            "is_archived": True,
                            "purpose": {"value": "Old room"},
                        },
                    ],
                    "response_metadata": {},
                }
            )
        )
        transport._app = SimpleNamespace(client=client)

        await transport._fetch_all_channels()

        assert transport._channels == {"C1": "#general"}
        assert transport._channel_ids == {"general": "C1"}
        assert transport._channel_info == {"C1": "General chat"}
        assert client.conversations_list.await_args.kwargs["exclude_archived"] is True

    asyncio.run(_run())


def test_fetch_all_channels_can_include_archived_channels() -> None:
    async def _run() -> None:
        transport = _make_transport(include_archived_channels=True)
        client = SimpleNamespace(
            conversations_list=AsyncMock(
                return_value={
                    "channels": [
                        {"id": "C1", "name": "general", "topic": {"value": "General chat"}},
                        {
                            "id": "C2",
                            "name": "archived-room",
                            "is_archived": True,
                            "purpose": {"value": "Old room"},
                        },
                    ],
                    "response_metadata": {},
                }
            )
        )
        transport._app = SimpleNamespace(client=client)

        await transport._fetch_all_channels()

        assert transport._channels == {
            "C1": "#general",
            "C2": "#archived-room",
        }
        assert client.conversations_list.await_args.kwargs["exclude_archived"] is False

    asyncio.run(_run())


def test_list_channels_supports_query_filtering() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._channels = {
            "C1": "#general",
            "C2": "#deployments",
        }
        transport._channel_info = {
            "C1": "Team chat",
            "C2": "Release updates",
        }
        transport._channel_ids = {"general": "C1", "deployments": "C2"}
        list_channels = _tool_func(transport, "list_channels")

        result = await list_channels(query="deploy")

        assert "#deployments" in result
        assert "#general" not in result

    asyncio.run(_run())


def test_list_channels_returns_no_channels_when_cache_empty() -> None:
    async def _run() -> None:
        transport = _make_transport()
        list_channels = _tool_func(transport, "list_channels")

        result = await list_channels()

        assert result == "No channels available."

    asyncio.run(_run())


def test_find_slack_users_merges_operator_identities() -> None:
    async def _run() -> None:
        store = SimpleNamespace(
            list_users=lambda: [
                SimpleNamespace(username="gavin.dev", identities=["slack:U1"], roles=["admin"])
            ],
            resolve_username=lambda platform_id: {"slack:U1": "gavin.dev"}.get(platform_id),
        )
        transport = _make_transport(store=store)
        transport._user_directory = {
            "U1": _make_user_profile("U1", "Gavin"),
            "U2": _make_user_profile("U2", "Avery"),
        }
        transport._users = {"U1": "Gavin", "U2": "Avery"}
        find_slack_users = _tool_func(transport, "find_slack_users")

        result = await find_slack_users(query="gavin.dev")

        assert "Mention `<@U1>`" in result
        assert "Operator `gavin.dev`" in result
        assert "U2" not in result

    asyncio.run(_run())


def test_find_slack_users_returns_no_users_when_cache_empty() -> None:
    async def _run() -> None:
        transport = _make_transport()
        find_slack_users = _tool_func(transport, "find_slack_users")

        result = await find_slack_users()

        assert result == "No Slack users available."

    asyncio.run(_run())


def test_prompt_extra_omits_channel_list_when_disabled() -> None:
    transport = _make_transport(inject_channels_into_prompt=False, inject_users_into_prompt=False)
    transport._channels = {"C1": "#general"}
    transport._channel_info = {"C1": "Team chat"}

    result = transport.get_prompt_extra()

    assert "## Channels" not in result
    assert "list_channels" in result
    assert "find_slack_users" in result
    assert "`@Name`" in result


def test_prompt_extra_injects_cached_channel_list_by_default() -> None:
    transport = _make_transport(inject_users_into_prompt=False)
    transport._channels = {"C1": "#general"}
    transport._channel_info = {"C1": "Team chat"}

    result = transport.get_prompt_extra()

    assert "## Channels" in result
    assert "- <#C1> #general — Team chat" in result


def test_prompt_extra_points_to_list_channels_when_injection_enabled_without_cache() -> None:
    transport = _make_transport(inject_users_into_prompt=False)

    result = transport.get_prompt_extra()

    assert "Channel names are not cached yet." in result
    assert "list_channels" in result
    assert "find_slack_users" in result


def test_prompt_extra_injects_user_directory() -> None:
    transport = _make_transport(inject_channels_into_prompt=False)
    transport._user_directory = {
        "U1": _make_user_profile("U1", "Gavin", is_bot=False),
        "U2": _make_user_profile("U2", "SlackBot", is_bot=True),
        "U3": _make_user_profile("U3", "Avery", is_bot=False),
        "U4": _make_user_profile("U4", "Deleted", is_bot=False, is_deleted=True),
    }

    result = transport.get_prompt_extra()

    assert "## Workspace Members" in result
    assert "- Avery <@U3>" in result
    assert "- Gavin <@U1>" in result
    assert "- SlackBot <@U2> (bot)" in result
    assert "Deleted" not in result
    # Check alphabetical order
    avery_pos = result.index("- Avery")
    gavin_pos = result.index("- Gavin")
    bot_pos = result.index("- SlackBot")
    assert avery_pos < gavin_pos < bot_pos


def test_prompt_extra_shows_fallback_when_user_directory_empty() -> None:
    transport = _make_transport(inject_channels_into_prompt=False)

    result = transport.get_prompt_extra()

    assert "User list not cached yet. Call `find_slack_users` if needed." in result


def test_prompt_extra_omits_users_when_disabled() -> None:
    transport = _make_transport(inject_users_into_prompt=False, inject_channels_into_prompt=False)
    transport._user_directory = {
        "U1": _make_user_profile("U1", "Gavin"),
    }

    result = transport.get_prompt_extra()

    assert "## Workspace Members" not in result
    assert "User list not cached yet" not in result


# --- Event-driven cache helper tests ---


def test_upsert_user_updates_cache() -> None:
    transport = _make_transport()
    raw_user = {
        "id": "U42",
        "name": "jdoe",
        "real_name": "Jane Doe",
        "profile": {"display_name": "Jane", "email": "jane@example.com"},
    }

    transport._upsert_user(raw_user)

    assert transport._users["U42"] == "Jane"
    profile = transport._user_directory["U42"]
    assert profile.display_name == "Jane"
    assert profile.real_name == "Jane Doe"
    assert profile.slack_name == "jdoe"
    assert profile.email == "jane@example.com"
    assert profile.is_bot is False
    assert profile.is_deleted is False


def test_upsert_user_ignores_empty_id() -> None:
    transport = _make_transport()
    transport._upsert_user({"name": "ghost"})
    assert transport._users == {}
    assert transport._user_directory == {}


def test_upsert_channel_updates_cache() -> None:
    transport = _make_transport()

    transport._upsert_channel("C99", "releases", topic="Deploy notes")

    assert transport._channels["C99"] == "#releases"
    assert transport._channel_ids["releases"] == "C99"
    assert transport._channel_info["C99"] == "Deploy notes"


def test_upsert_channel_clears_info_when_empty() -> None:
    transport = _make_transport()
    transport._channel_info["C99"] = "old topic"
    transport._channels["C99"] = "#releases"
    transport._channel_ids["releases"] = "C99"

    transport._upsert_channel("C99", "releases")

    assert "C99" not in transport._channel_info


def test_upsert_channel_ignores_empty_id_or_name() -> None:
    transport = _make_transport()
    transport._upsert_channel("", "test")
    transport._upsert_channel("C1", "")
    assert transport._channels == {}


def test_remove_channel_clears_all_dicts() -> None:
    transport = _make_transport()
    transport._channels["C5"] = "#old-channel"
    transport._channel_ids["old-channel"] = "C5"
    transport._channel_info["C5"] = "some topic"

    transport._remove_channel("C5")

    assert "C5" not in transport._channels
    assert "old-channel" not in transport._channel_ids
    assert "C5" not in transport._channel_info


def test_remove_channel_noop_for_unknown_id() -> None:
    transport = _make_transport()
    transport._remove_channel("CXXX")
    assert transport._channels == {}


def test_event_handler_team_join(monkeypatch) -> None:
    captured_handlers: dict[str, object] = {}

    class FakeApp:
        def __init__(self, token: str):
            self.token = token
            self.client = SimpleNamespace()

        def event(self, name: str):
            def _decorator(func):
                captured_handlers[name] = func
                return func

            return _decorator

    class FakeHandler:
        def __init__(self, app, token):
            pass

        async def start_async(self):
            return None

        async def close_async(self):
            return None

    async def _run() -> None:
        transport = _make_transport()
        monkeypatch.setattr("operator_ai.transport.slack.AsyncApp", FakeApp)
        monkeypatch.setattr("operator_ai.transport.slack.AsyncSocketModeHandler", FakeHandler)
        transport._fetch_all_users = AsyncMock()  # type: ignore[method-assign]
        transport._fetch_all_channels = AsyncMock()  # type: ignore[method-assign]
        await transport.start(AsyncMock())

        handler = captured_handlers["team_join"]
        await handler(
            {
                "user": {
                    "id": "UNEW",
                    "name": "newbie",
                    "real_name": "New Person",
                    "profile": {"display_name": "Newbie"},
                }
            },
            None,
        )

        assert transport._users["UNEW"] == "Newbie"
        assert transport._user_directory["UNEW"].display_name == "Newbie"
        await transport.stop()

    asyncio.run(_run())


def test_event_handler_channel_archive(monkeypatch) -> None:
    captured_handlers: dict[str, object] = {}

    class FakeApp:
        def __init__(self, token: str):
            self.token = token
            self.client = SimpleNamespace()

        def event(self, name: str):
            def _decorator(func):
                captured_handlers[name] = func
                return func

            return _decorator

    class FakeHandler:
        def __init__(self, app, token):
            pass

        async def start_async(self):
            return None

        async def close_async(self):
            return None

    async def _run() -> None:
        transport = _make_transport()
        monkeypatch.setattr("operator_ai.transport.slack.AsyncApp", FakeApp)
        monkeypatch.setattr("operator_ai.transport.slack.AsyncSocketModeHandler", FakeHandler)
        transport._fetch_all_users = AsyncMock()  # type: ignore[method-assign]
        transport._fetch_all_channels = AsyncMock()  # type: ignore[method-assign]
        await transport.start(AsyncMock())

        # Pre-populate the channel cache
        transport._channels["C77"] = "#old-stuff"
        transport._channel_ids["old-stuff"] = "C77"
        transport._channel_info["C77"] = "topic"

        handler = captured_handlers["channel_archive"]
        await handler({"channel": "C77"}, None)

        assert "C77" not in transport._channels
        assert "old-stuff" not in transport._channel_ids
        assert "C77" not in transport._channel_info
        await transport.stop()

    asyncio.run(_run())


def test_resolve_channel_id_returns_cached_name() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._channel_ids["general"] = "C1"

        result = await transport.resolve_channel_id("#general")

        assert result == "C1"

    asyncio.run(_run())


def test_resolve_channel_id_returns_none_for_unknown() -> None:
    async def _run() -> None:
        transport = _make_transport()

        result = await transport.resolve_channel_id("#nonexistent")

        assert result is None

    asyncio.run(_run())


def test_resolve_channel_id_passes_through_raw_ids() -> None:
    async def _run() -> None:
        transport = _make_transport()

        assert await transport.resolve_channel_id("C123") == "C123"
        assert await transport.resolve_channel_id("G456") == "G456"
        assert await transport.resolve_channel_id("D789") == "D789"

    asyncio.run(_run())


# --- Enriched message context tests ---


def test_dispatch_sets_was_mentioned_for_app_mention() -> None:
    captured: list[IncomingMessage] = []

    async def _run() -> None:
        transport = _make_transport()
        transport._users["UBOT"] = "Bot"

        async def on_message(msg: IncomingMessage) -> None:
            captured.append(msg)

        # Simulate app_mention dispatch (strip_leading_mention=True)
        await transport._dispatch(
            {
                "text": "<@UBOT> hello",
                "channel": "C123",
                "ts": "1234567890.123456",
                "user": "U999",
                "channel_type": "channel",
            },
            on_message,
            strip_leading_mention=True,
        )

        # Simulate DM dispatch (strip_leading_mention=False)
        await transport._dispatch(
            {
                "text": "hello",
                "channel": "D456",
                "ts": "1234567890.654321",
                "user": "U999",
                "channel_type": "im",
            },
            on_message,
        )

    asyncio.run(_run())

    assert len(captured) == 2
    assert captured[0].was_mentioned is True
    assert captured[1].was_mentioned is False


def test_to_prompt_includes_chat_type_when_set() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
        chat_type="channel",
    )
    result = ctx.to_prompt()
    assert "- Chat type: channel" in result
    # Verify ordering: platform before chat_type before channel
    platform_pos = result.index("- Platform: slack")
    chat_type_pos = result.index("- Chat type: channel")
    channel_pos = result.index("- Channel: #general")
    assert platform_pos < chat_type_pos < channel_pos


def test_to_prompt_omits_chat_type_when_empty() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
    )
    result = ctx.to_prompt()
    assert "Chat type" not in result


# --- Outbound mention resolution tests ---


def test_resolve_outbound_mentions_replaces_at_name() -> None:
    transport = _make_transport()
    transport._user_directory = {"U1": _make_user_profile("U1", "Gavin")}
    assert transport._resolve_outbound_mentions("Hey @Gavin!") == "Hey <@U1>!"


def test_resolve_outbound_mentions_replaces_channel() -> None:
    transport = _make_transport()
    transport._channel_ids = {"general": "C1"}
    assert transport._resolve_outbound_mentions("Post in #general") == "Post in <#C1>"


def test_resolve_outbound_mentions_skips_already_resolved() -> None:
    transport = _make_transport()
    transport._user_directory = {"U1": _make_user_profile("U1", "Gavin")}
    transport._channel_ids = {"general": "C1"}
    text = "Hey <@U1> check <#C1>"
    assert transport._resolve_outbound_mentions(text) == text


def test_resolve_outbound_mentions_case_insensitive() -> None:
    transport = _make_transport()
    transport._user_directory = {"U1": _make_user_profile("U1", "Gavin")}
    assert transport._resolve_outbound_mentions("Hi @gavin") == "Hi <@U1>"


def test_resolve_outbound_mentions_no_match_leaves_as_is() -> None:
    transport = _make_transport()
    transport._user_directory = {"U1": _make_user_profile("U1", "Gavin")}
    assert transport._resolve_outbound_mentions("Hi @Unknown") == "Hi @Unknown"


def test_resolve_outbound_mentions_no_partial_match() -> None:
    transport = _make_transport()
    transport._user_directory = {"U1": _make_user_profile("U1", "gavin")}
    text = "email@gavin.com"
    assert transport._resolve_outbound_mentions(text) == text


def test_resolve_outbound_mentions_longest_match_first() -> None:
    transport = _make_transport()
    transport._user_directory = {
        "U1": _make_user_profile("U1", "Gavin"),
        "U2": _make_user_profile("U2", "Gavin Vickery"),
    }
    result = transport._resolve_outbound_mentions("Hey @Gavin Vickery!")
    assert result == "Hey <@U2>!"


def test_resolve_outbound_mentions_skips_code_blocks() -> None:
    transport = _make_transport()
    transport._user_directory = {"U1": _make_user_profile("U1", "Gavin")}
    transport._channel_ids = {"general": "C1"}

    # Inline code
    assert transport._resolve_outbound_mentions("Use `@Gavin` syntax") == "Use `@Gavin` syntax"
    # Fenced code block
    text = "Example:\n```\n@Gavin in #general\n```\nDone"
    result = transport._resolve_outbound_mentions(text)
    assert "@Gavin" in result  # still inside code block
    assert "#general" in result  # still inside code block
    assert result.startswith("Example:")
    assert result.endswith("Done")

    # Mixed: code + real mention
    text = "Hey @Gavin, run `@Gavin` to test"
    result = transport._resolve_outbound_mentions(text)
    assert result == "Hey <@U1>, run `@Gavin` to test"


def test_resolve_outbound_mentions_disabled_by_config() -> None:
    transport = _make_transport(expand_mentions=False)
    transport._user_directory = {"U1": _make_user_profile("U1", "Gavin")}
    assert transport._resolve_outbound_mentions("Hey @Gavin") == "Hey @Gavin"


def test_resolve_context_sets_chat_type() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._channels["D123"] = "DM"
        transport._channels["C456"] = "#general"
        transport._channels["G789"] = "#group"
        transport._users["UTEST"] = "Test User"

        dm_msg = IncomingMessage(
            text="hi",
            user_id="slack:UTEST",
            channel_id="D123",
            message_id="1.1",
            root_message_id="1.1",
            transport_name="slack",
        )
        channel_msg = IncomingMessage(
            text="hi",
            user_id="slack:UTEST",
            channel_id="C456",
            message_id="2.1",
            root_message_id="2.1",
            transport_name="slack",
        )
        group_msg = IncomingMessage(
            text="hi",
            user_id="slack:UTEST",
            channel_id="G789",
            message_id="3.1",
            root_message_id="3.1",
            transport_name="slack",
        )

        dm_ctx = await transport.resolve_context(dm_msg)
        channel_ctx = await transport.resolve_context(channel_msg)
        group_ctx = await transport.resolve_context(group_msg)

        assert dm_ctx.chat_type == "dm"
        assert channel_ctx.chat_type == "channel"
        assert group_ctx.chat_type == "group"

    asyncio.run(_run())


# ============================================================
# Phase 7: Transport base / CLI / registry tests
# ============================================================


# --- IncomingMessage dataclass ---


def test_incoming_message_creation() -> None:
    msg = IncomingMessage(
        text="hello",
        user_id="U123",
        channel_id="C456",
        message_id="msg-1",
        root_message_id="root-1",
        transport_name="slack",
    )
    assert msg.text == "hello"
    assert msg.user_id == "U123"
    assert msg.channel_id == "C456"
    assert msg.message_id == "msg-1"
    assert msg.root_message_id == "root-1"
    assert msg.transport_name == "slack"
    assert msg.is_private is False
    assert msg.was_mentioned is False
    assert msg.attachments == []
    assert msg.created_at is None


def test_incoming_message_with_all_fields() -> None:
    now = datetime.now(tz=UTC)
    attachment = Attachment(
        filename="test.png",
        content_type="image/png",
        size=1024,
        url="https://example.com/test.png",
        platform_id="F123",
    )
    msg = IncomingMessage(
        text="check this",
        user_id="U99",
        channel_id="D100",
        message_id="m-2",
        root_message_id="m-1",
        transport_name="cli",
        is_private=True,
        was_mentioned=True,
        attachments=[attachment],
        created_at=now,
    )
    assert msg.is_private is True
    assert msg.was_mentioned is True
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "test.png"
    assert msg.created_at == now


# --- Attachment dataclass ---


def test_attachment_creation() -> None:
    a = Attachment(
        filename="doc.pdf",
        content_type="application/pdf",
        size=2048,
        url="https://files.example.com/doc.pdf",
    )
    assert a.filename == "doc.pdf"
    assert a.content_type == "application/pdf"
    assert a.size == 2048
    assert a.platform_id == ""


# --- MessageContext.to_prompt ---


def test_to_prompt_all_fields_rendered() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U456",
        user_name="Alice",
        username="alice",
        chat_type="channel",
    )
    result = ctx.to_prompt(workspace="/home/agent/workspace", operator_home="/home/.operator")
    assert "# Context" in result
    assert "- Platform: slack" in result
    assert "- Chat type: channel" in result
    assert "- Channel: #general (`C123`)" in result
    assert "- User: alice (Alice via slack)" in result
    assert "- Workspace: `/home/agent/workspace`" in result
    assert "- Operator home: `/home/.operator` (also `$OPERATOR_HOME`)" in result


def test_to_prompt_minimal_fields() -> None:
    ctx = MessageContext(
        platform="cli",
        channel_id="cli",
        channel_name="cli",
        user_id="cli",
        user_name="cli",
    )
    result = ctx.to_prompt()
    assert "# Context" in result
    assert "- Platform: cli" in result
    assert "- Channel: cli (`cli`)" in result
    assert "- User: cli (`cli`)" in result
    assert "Chat type" not in result
    assert "Workspace" not in result
    assert "Operator home" not in result


# --- Transport.build_conversation_id ---


def test_build_conversation_id_format() -> None:
    cli = CliTransport(agent_name="hermy")
    msg = IncomingMessage(
        text="test",
        user_id="cli",
        channel_id="C123",
        message_id="m-1",
        root_message_id="root-42",
        transport_name="hermy",
    )
    result = cli.build_conversation_id(msg)
    assert result == "cli:hermy:C123:root-42"


def test_build_conversation_id_uses_root_message_id() -> None:
    cli = CliTransport(agent_name="cora")
    msg1 = IncomingMessage(
        text="a",
        user_id="u",
        channel_id="C1",
        message_id="m1",
        root_message_id="thread-1",
        transport_name="cora",
    )
    msg2 = IncomingMessage(
        text="b",
        user_id="u",
        channel_id="C1",
        message_id="m2",
        root_message_id="thread-1",
        transport_name="cora",
    )
    # Same thread -> same conversation ID
    assert cli.build_conversation_id(msg1) == cli.build_conversation_id(msg2)


# --- Transport base defaults ---


def test_transport_base_get_tools_returns_empty() -> None:
    cli = CliTransport(agent_name="test")
    assert cli.get_tools() == []


def test_transport_base_get_thread_context_returns_none() -> None:
    cli = CliTransport(agent_name="test")
    msg = IncomingMessage(
        text="hi",
        user_id="u",
        channel_id="c",
        message_id="m",
        root_message_id="r",
        transport_name="test",
    )
    result = asyncio.run(cli.get_thread_context(msg))
    assert result is None


def test_transport_base_download_file_raises() -> None:
    cli = CliTransport(agent_name="test")
    attachment = Attachment(filename="f.txt", content_type="text/plain", size=100, url="http://x")
    with pytest.raises(NotImplementedError, match="CliTransport"):
        asyncio.run(cli.download_file(attachment))


def test_transport_base_send_file_raises() -> None:
    cli = CliTransport(agent_name="test")
    with pytest.raises(NotImplementedError, match="CliTransport"):
        asyncio.run(cli.send_file("C1", b"data", "file.txt"))


def test_transport_base_get_prompt_extra_returns_empty() -> None:
    cli = CliTransport(agent_name="test")
    assert cli.get_prompt_extra() == ""


def test_transport_base_update_is_noop() -> None:
    cli = CliTransport(agent_name="test")
    # Should not raise
    asyncio.run(cli.update("C1", "m1", "new text"))


def test_transport_base_delete_is_noop() -> None:
    cli = CliTransport(agent_name="test")
    # Should not raise
    asyncio.run(cli.delete("C1", "m1"))


def test_transport_base_resolve_channel_id_passthrough() -> None:
    cli = CliTransport(agent_name="test")
    result = asyncio.run(cli.resolve_channel_id("anything"))
    assert result == "anything"


# --- CliTransport ---


def test_cli_transport_send_prints_to_stderr() -> None:
    async def _run() -> str:
        cli = CliTransport(agent_name="hermy")
        old_stderr = sys.stderr
        capture = io.StringIO()
        sys.stderr = capture
        try:
            msg_id = await cli.send("C123", "Hello world")
        finally:
            sys.stderr = old_stderr
        return capture.getvalue() + "|" + msg_id

    output = asyncio.run(_run())
    parts = output.split("|")
    stderr_output = parts[0]
    msg_id = parts[1]
    assert "[send_message" in stderr_output
    assert "C123" in stderr_output
    assert "Hello world" in stderr_output
    assert msg_id == "cli-1"


def test_cli_transport_send_with_thread_id() -> None:
    async def _run() -> str:
        cli = CliTransport(agent_name="hermy")
        old_stderr = sys.stderr
        capture = io.StringIO()
        sys.stderr = capture
        try:
            await cli.send("C123", "Reply", thread_id="t-42")
        finally:
            sys.stderr = old_stderr
        return capture.getvalue()

    output = asyncio.run(_run())
    assert "(thread: t-42)" in output


def test_cli_transport_message_counter_increments() -> None:
    async def _run() -> list[str]:
        cli = CliTransport(agent_name="hermy")
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            ids = []
            for _ in range(3):
                ids.append(await cli.send("C1", "msg"))
            return ids
        finally:
            sys.stderr = old_stderr

    ids = asyncio.run(_run())
    assert ids == ["cli-1", "cli-2", "cli-3"]


def test_cli_transport_resolve_context_returns_cli_values() -> None:
    async def _run() -> MessageContext:
        cli = CliTransport(agent_name="hermy")
        msg = IncomingMessage(
            text="test",
            user_id="whoever",
            channel_id="wherever",
            message_id="m1",
            root_message_id="m1",
            transport_name="hermy",
        )
        return await cli.resolve_context(msg)

    ctx = asyncio.run(_run())
    assert ctx.platform == "cli"
    assert ctx.channel_id == "cli"
    assert ctx.channel_name == "cli"
    assert ctx.user_id == "cli"
    assert ctx.user_name == "cli"


def test_cli_transport_start_raises() -> None:
    cli = CliTransport(agent_name="test")
    with pytest.raises(NotImplementedError, match="does not accept inbound"):
        asyncio.run(cli.start(AsyncMock()))


def test_cli_transport_platform_and_name() -> None:
    cli = CliTransport(agent_name="hermy")
    assert cli.platform == "cli"
    assert cli.name == "hermy"
    assert cli.agent_name == "hermy"


# --- Transport registry ---


def test_normalize_transport_options_slack() -> None:
    options = {
        "bot_token_env": "MY_BOT_TOKEN",
        "app_token_env": "MY_APP_TOKEN",
    }
    result = normalize_transport_options("slack", options)
    assert result["bot_token_env"] == "MY_BOT_TOKEN"
    assert result["app_token_env"] == "MY_APP_TOKEN"
    # Defaults should be populated
    assert result["include_archived_channels"] is False
    assert result["inject_channels_into_prompt"] is True
    assert result["inject_users_into_prompt"] is True
    assert result["expand_mentions"] is True


def test_normalize_transport_options_unknown_type_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported transport type"):
        normalize_transport_options("telegram", {})


def test_normalize_transport_options_rejects_extra_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="extra_forbidden"):
        normalize_transport_options(
            "slack",
            {
                "bot_token_env": "TOK",
                "app_token_env": "APP",
                "unknown_field": True,
            },
        )


def test_transport_logger_names_includes_slack() -> None:
    names = transport_logger_names()
    assert "slack_bolt" in names
    assert "slack_sdk" in names


def test_transport_logger_names_returns_tuple() -> None:
    names = transport_logger_names()
    assert isinstance(names, tuple)


# --- No memory code in transport layer (structural verification) ---


def test_transport_base_has_no_memory_imports() -> None:
    """Verify the transport base module does not import from memory modules."""
    import operator_ai.transport.base as base_mod

    source = Path(base_mod.__file__).read_text()
    assert "from operator_ai.memory" not in source
    assert "import operator_ai.memory" not in source


def test_cli_transport_has_no_memory_imports() -> None:
    """Verify the CLI transport module does not import from memory modules."""
    import operator_ai.transport.cli as cli_mod

    source = Path(cli_mod.__file__).read_text()
    assert "from operator_ai.memory" not in source
    assert "import operator_ai.memory" not in source


def test_transport_abc_defines_required_interface() -> None:
    """Verify the Transport ABC defines the expected interface methods."""
    import inspect

    methods = {name for name, _ in inspect.getmembers(Transport, predicate=inspect.isfunction)}
    expected = {
        "start",
        "stop",
        "send",
        "resolve_context",
        "build_conversation_id",
        "get_tools",
        "get_thread_context",
        "download_file",
        "send_file",
        "get_prompt_extra",
        "update",
        "delete",
        "resolve_channel_id",
    }
    assert expected.issubset(methods)
