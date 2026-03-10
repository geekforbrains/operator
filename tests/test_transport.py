from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import operator_ai.tools  # noqa: F401  — warm up circular imports
from operator_ai.transport.base import MessageContext
from operator_ai.transport.slack import SlackTransport


def _make_transport(**kwargs: object) -> SlackTransport:
    return SlackTransport(
        name="slack",
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
        **kwargs,
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


def test_slack_start_warms_channel_cache_before_handler_blocks(monkeypatch) -> None:
    state = {"warmed": False, "start_called": False}

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
            assert state["warmed"] is True
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
        transport._refresh_channel_cache = AsyncMock()  # type: ignore[method-assign]

        async def _warm(*, force: bool = False) -> None:
            assert force is True
            state["warmed"] = True

        transport._refresh_channel_cache.side_effect = _warm
        await transport.start(_noop)
        await transport.stop()
        transport._refresh_channel_cache.assert_awaited_once_with(force=True)

    asyncio.run(_run())

    assert state["start_called"] is True


def test_slack_channel_warmup_can_be_disabled(monkeypatch) -> None:
    called = {"start_called": False}

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
            called["start_called"] = True
            return None

        async def close_async(self) -> None:
            return None

    async def _noop(_msg) -> None:
        return None

    async def _run() -> None:
        transport = _make_transport(warm_channels_on_startup=False)
        monkeypatch.setattr("operator_ai.transport.slack.AsyncApp", FakeApp)
        monkeypatch.setattr("operator_ai.transport.slack.AsyncSocketModeHandler", FakeHandler)
        transport._refresh_channel_cache = AsyncMock()  # type: ignore[method-assign]
        await transport.start(_noop)
        await transport.stop()
        transport._refresh_channel_cache.assert_not_awaited()

    asyncio.run(_run())

    assert called["start_called"] is True


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


def test_ensure_channel_cache_fresh_refreshes_when_empty() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._fetch_all_channels = AsyncMock()  # type: ignore[method-assign]

        await transport._ensure_channel_cache_fresh()

        transport._fetch_all_channels.assert_awaited_once()

    asyncio.run(_run())


def test_ensure_channel_cache_fresh_skips_refresh_when_cache_is_fresh() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._channel_cache_refreshed_at = time.monotonic()
        transport._fetch_all_channels = AsyncMock()  # type: ignore[method-assign]

        await transport._ensure_channel_cache_fresh()

        transport._fetch_all_channels.assert_not_awaited()

    asyncio.run(_run())


def test_resolve_channel_id_forces_one_refresh_on_cache_miss() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._channel_cache_refreshed_at = time.monotonic()
        transport._ensure_channel_cache_fresh = AsyncMock()  # type: ignore[method-assign]

        async def _force_refresh(*, force: bool = False) -> None:
            assert force is True
            transport._channel_ids["deployments"] = "C9"

        transport._refresh_channel_cache = AsyncMock(side_effect=_force_refresh)  # type: ignore[method-assign]

        result = await transport.resolve_channel_id("#deployments")

        assert result == "C9"
        transport._ensure_channel_cache_fresh.assert_awaited_once()
        transport._refresh_channel_cache.assert_awaited_once_with(force=True)

    asyncio.run(_run())


def test_list_channels_refreshes_on_first_use_and_supports_query_filtering() -> None:
    async def _run() -> None:
        transport = _make_transport()
        client = SimpleNamespace(
            conversations_list=AsyncMock(
                return_value={
                    "channels": [
                        {"id": "C1", "name": "general", "topic": {"value": "Team chat"}},
                        {
                            "id": "C2",
                            "name": "deployments",
                            "purpose": {"value": "Release updates"},
                        },
                    ],
                    "response_metadata": {},
                }
            )
        )
        transport._app = SimpleNamespace(client=client)
        list_channels = _tool_func(transport, "list_channels")

        result = await list_channels(query="deploy")

        assert "#deployments" in result
        assert "#general" not in result

    asyncio.run(_run())


def test_list_channels_returns_error_when_refresh_fails_without_cache() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._ensure_channel_cache_fresh = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        list_channels = _tool_func(transport, "list_channels")

        result = await list_channels()

        assert result == "[error: failed to load Slack channels]"

    asyncio.run(_run())


def test_list_channels_falls_back_to_cached_snapshot_on_refresh_failure() -> None:
    async def _run() -> None:
        transport = _make_transport()
        transport._channels = {"C1": "#general"}
        transport._channel_info = {"C1": "Team chat"}
        transport._ensure_channel_cache_fresh = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        list_channels = _tool_func(transport, "list_channels")

        result = await list_channels()

        assert result == "- #general (`C1`) — Team chat"

    asyncio.run(_run())


def test_prompt_extra_omits_channel_list_by_default() -> None:
    transport = _make_transport()
    transport._channels = {"C1": "#general"}
    transport._channel_info = {"C1": "Team chat"}

    result = transport.get_prompt_extra()

    assert "# Available Channels" not in result
    assert "list_channels" in result


def test_prompt_extra_can_inject_cached_channel_list() -> None:
    transport = _make_transport(inject_channels_into_prompt=True)
    transport._channels = {"C1": "#general"}
    transport._channel_info = {"C1": "Team chat"}

    result = transport.get_prompt_extra()

    assert "# Available Channels" in result
    assert "- #general (`C1`) — Team chat" in result


def test_prompt_extra_points_to_list_channels_when_injection_enabled_without_cache() -> None:
    transport = _make_transport(inject_channels_into_prompt=True)

    result = transport.get_prompt_extra()

    assert "Channel names are not cached yet." in result
    assert "list_channels" in result
