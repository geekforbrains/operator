from __future__ import annotations

import asyncio

import operator_ai.tools  # noqa: F401  — warm up circular imports
from operator_ai.transport.base import IncomingMessage
from operator_ai.transport.slack import SlackTransport, SlackUserProfile


def test_slack_message_formatting_uses_utc() -> None:
    transport = SlackTransport(
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )

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

    assert "[Gavin] 12:00 PM: Hello" in formatted


def test_slack_conversation_ids_are_thread_scoped_sessions() -> None:
    transport = SlackTransport(
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )

    first = IncomingMessage(
        text="hello",
        user_id="slack:U123",
        channel_id="D123",
        message_id="1701.1",
        root_message_id="1701.1",
        transport_name="slack",
        is_private=True,
    )
    reply = IncomingMessage(
        text="follow-up",
        user_id="slack:U123",
        channel_id="D123",
        message_id="1701.2",
        root_message_id="1701.1",
        transport_name="slack",
        is_private=True,
    )
    second = IncomingMessage(
        text="new topic",
        user_id="slack:U123",
        channel_id="D123",
        message_id="1702.1",
        root_message_id="1702.1",
        transport_name="slack",
        is_private=True,
    )

    assert transport.build_conversation_id(first) == "slack:operator:D123:1701.1"
    assert transport.build_conversation_id(reply) == transport.build_conversation_id(first)
    assert transport.build_conversation_id(second) != transport.build_conversation_id(first)


def test_slack_prompt_extra_documents_thread_scoped_sessions() -> None:
    transport = SlackTransport(
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )

    prompt = transport.get_prompt_extra()

    assert "thread-scoped" in prompt
    assert "mention you" in prompt
    assert "unambiguous" in prompt
    assert "slack_read_channel" in prompt
    assert "slack_read_thread" in prompt


def test_slack_outbound_mentions_expand_unique_display_names() -> None:
    transport = SlackTransport(
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    transport._user_directory = {
        "U123": SlackUserProfile(
            user_id="U123",
            slack_name="alice",
            display_name="Alice",
            real_name="Alice Example",
            email="alice@example.com",
            is_bot=False,
            is_deleted=False,
        )
    }

    assert transport._resolve_outbound_mentions("Ping @Alice") == "Ping <@U123>"


def test_slack_outbound_mentions_skip_ambiguous_display_names() -> None:
    transport = SlackTransport(
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    transport._user_directory = {
        "U123": SlackUserProfile(
            user_id="U123",
            slack_name="alex.one",
            display_name="Alex",
            real_name="Alex One",
            email="alex.one@example.com",
            is_bot=False,
            is_deleted=False,
        ),
        "U456": SlackUserProfile(
            user_id="U456",
            slack_name="alex.two",
            display_name="Alex",
            real_name="Alex Two",
            email="alex.two@example.com",
            is_bot=False,
            is_deleted=False,
        ),
        "U789": SlackUserProfile(
            user_id="U789",
            slack_name="casey",
            display_name="Casey",
            real_name="Casey Example",
            email="casey@example.com",
            is_bot=False,
            is_deleted=False,
        ),
    }

    assert transport._resolve_outbound_mentions("Ping @Alex and @Casey") == "Ping @Alex and <@U789>"


def test_slack_channel_snapshot_refresh_replaces_old_names(monkeypatch) -> None:
    async def _run() -> None:
        transport = SlackTransport(
            agent_name="operator",
            bot_token="xoxb-test",
            app_token="xapp-test",
        )

        class _FakeClient:
            def __init__(self) -> None:
                self._responses = [
                    {
                        "channels": [
                            {
                                "id": "C1",
                                "name": "general",
                                "topic": {"value": ""},
                                "purpose": {"value": ""},
                            }
                        ],
                        "response_metadata": {},
                    },
                    {
                        "channels": [
                            {
                                "id": "C1",
                                "name": "eng",
                                "topic": {"value": ""},
                                "purpose": {"value": ""},
                            }
                        ],
                        "response_metadata": {},
                    },
                ]

            async def conversations_list(self, **kwargs) -> dict:  # noqa: ARG002
                return self._responses.pop(0)

        class _FakeApp:
            def __init__(self) -> None:
                self.client = _FakeClient()

        transport._app = _FakeApp()  # type: ignore[assignment]

        async def _passthrough_api_call(operation: str, call) -> dict:  # noqa: ARG001
            return await call()

        monkeypatch.setattr("operator_ai.transport.slack.api.api_call", _passthrough_api_call)

        await transport._refresh_channels()
        assert await transport.resolve_channel_id("#general") == "C1"
        assert transport._format_channel_list() == ["- <#C1> #general"]

        await transport._refresh_channels()
        assert await transport.resolve_channel_id("#general") is None
        assert await transport.resolve_channel_id("#eng") == "C1"
        assert transport._format_channel_list() == ["- <#C1> #eng"]

    asyncio.run(_run())


def test_slack_channel_events_refresh_full_snapshot(monkeypatch) -> None:
    async def _run() -> None:
        handlers: dict[str, object] = {}

        class _FakeClient:
            async def auth_test(self) -> dict[str, str]:
                return {"user_id": "UBOT"}

        class _FakeApp:
            def __init__(self, token: str) -> None:
                self.token = token
                self.client = _FakeClient()

            def event(self, name: str):
                def _register(func):
                    handlers[name] = func
                    return func

                return _register

        class _FakeHandler:
            def __init__(self, app, app_token: str) -> None:
                self.app = app
                self.app_token = app_token

            async def start_async(self) -> None:
                return None

            async def close_async(self) -> None:
                return None

        monkeypatch.setattr("operator_ai.transport.slack.transport.AsyncApp", _FakeApp)
        monkeypatch.setattr(
            "operator_ai.transport.slack.transport.AsyncSocketModeHandler", _FakeHandler
        )

        transport = SlackTransport(
            agent_name="operator",
            bot_token="xoxb-test",
            app_token="xapp-test",
        )

        async def _fake_api_call(operation: str, _call) -> dict:
            if operation == "auth.test":
                return {"user_id": "UBOT"}
            return {}

        refresh_calls: list[str] = []

        async def _tracking_refresh() -> None:
            refresh_calls.append("refresh")

        async def _noop() -> None:
            return None

        monkeypatch.setattr(transport, "_api_call", _fake_api_call)
        monkeypatch.setattr(
            "operator_ai.transport.slack.api.fetch_all_users",
            lambda *_a, **_kw: _noop(),
        )
        monkeypatch.setattr(transport, "_refresh_channels", _tracking_refresh)

        async def _on_message(msg: IncomingMessage) -> None:  # noqa: ARG001
            return None

        await transport.start(_on_message)
        refresh_calls.clear()

        # Fire four channel events rapidly — debounce should coalesce into one refresh
        await handlers["channel_created"]({"channel": {"id": "C1", "name": "general"}}, None)  # type: ignore[operator]
        await handlers["channel_rename"]({"channel": {"id": "C1", "name": "eng"}}, None)  # type: ignore[operator]
        await handlers["channel_archive"]({"channel": "C1"}, None)  # type: ignore[operator]
        await handlers["channel_unarchive"]({"channel": "C1"}, None)  # type: ignore[operator]

        assert refresh_calls == [], "debounce should delay the refresh"

        # Wait for debounce to fire
        await asyncio.sleep(transport._CHANNEL_REFRESH_DELAY + 0.1)
        # Let the scheduled task run
        await asyncio.sleep(0)

        assert refresh_calls == ["refresh"], "rapid events should coalesce into a single refresh"

        await transport.stop()

    asyncio.run(_run())


def test_slack_channel_replies_require_mentions(monkeypatch) -> None:
    async def _run() -> None:
        handlers: dict[str, object] = {}

        class _FakeClient:
            async def auth_test(self) -> dict[str, str]:
                return {"user_id": "UBOT"}

        class _FakeApp:
            def __init__(self, token: str) -> None:
                self.token = token
                self.client = _FakeClient()

            def event(self, name: str):
                def _register(func):
                    handlers[name] = func
                    return func

                return _register

        class _FakeHandler:
            def __init__(self, app, app_token: str) -> None:
                self.app = app
                self.app_token = app_token

            async def start_async(self) -> None:
                return None

            async def close_async(self) -> None:
                return None

        monkeypatch.setattr("operator_ai.transport.slack.transport.AsyncApp", _FakeApp)
        monkeypatch.setattr(
            "operator_ai.transport.slack.transport.AsyncSocketModeHandler", _FakeHandler
        )

        transport = SlackTransport(
            agent_name="operator",
            bot_token="xoxb-test",
            app_token="xapp-test",
        )

        async def _fake_api_call(operation: str, _call) -> dict:
            if operation == "auth.test":
                return {"user_id": "UBOT"}
            return {}

        async def _noop() -> None:
            return None

        monkeypatch.setattr(transport, "_api_call", _fake_api_call)
        monkeypatch.setattr(
            "operator_ai.transport.slack.api.fetch_all_users",
            lambda *_a, **_kw: _noop(),
        )
        monkeypatch.setattr(transport, "_refresh_channels", _noop)

        seen: list[IncomingMessage] = []

        async def _on_message(msg: IncomingMessage) -> None:
            seen.append(msg)

        scheduled: list[asyncio.Task[None]] = []

        def _capture_task(coro) -> None:
            scheduled.append(asyncio.create_task(coro))

        monkeypatch.setattr(transport, "_create_task", _capture_task)

        await transport.start(_on_message)

        app_mention = handlers["app_mention"]
        await app_mention(  # type: ignore[operator]
            {
                "type": "app_mention",
                "channel_type": "channel",
                "channel": "C1",
                "ts": "1701.1",
                "user": "U123",
                "text": "summarize this",
            },
            None,
        )
        await asyncio.gather(*scheduled)
        scheduled.clear()

        assert len(seen) == 1
        assert seen[0].message_id == "1701.1"

        message = handlers["message"]
        await message(  # type: ignore[operator]
            {
                "type": "message",
                "channel_type": "channel",
                "channel": "C1",
                "thread_ts": "1701.1",
                "ts": "1701.2",
                "user": "U123",
                "text": "also include action items",
            },
            None,
        )

        assert not scheduled
        assert len(seen) == 1

        await transport.stop()

    asyncio.run(_run())
