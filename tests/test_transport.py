from __future__ import annotations

import asyncio

import operator_ai.tools  # noqa: F401  — warm up circular imports
from operator_ai.transport.base import MessageContext
from operator_ai.transport.slack import SlackTransport


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
    assert "- Username: gavin" in result
    assert "- Name: Gavin Vickery" in result


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


def test_to_prompt_renders_roles() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
        username="gavin",
        roles=["admin", "developer"],
    )
    result = ctx.to_prompt()
    assert "- Roles: admin, developer" in result


def test_to_prompt_renders_timezone() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
        username="gavin",
        timezone="America/Vancouver",
    )
    result = ctx.to_prompt()
    assert "- Timezone: America/Vancouver" in result
    assert "not set" not in result


def test_to_prompt_timezone_not_set_with_username() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
        username="gavin",
    )
    result = ctx.to_prompt()
    assert "- Timezone: *not set" in result


def test_to_prompt_no_timezone_note_without_username() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
    )
    result = ctx.to_prompt()
    assert "Timezone" not in result


def test_to_prompt_renders_agent_identity() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
        agent_name="hermy",
        agent_platform_id="U0AF0SK8HPU",
    )
    result = ctx.to_prompt()
    assert "- Agent (You): hermy (`U0AF0SK8HPU`)" in result


def test_to_prompt_agent_without_platform_id() -> None:
    ctx = MessageContext(
        platform="cli",
        channel_id="cli",
        channel_name="cli",
        user_id="cli",
        user_name="cli",
        agent_name="operator",
    )
    result = ctx.to_prompt()
    assert "- Agent (You): operator" in result
    assert "(`" not in result.split("Agent (You):")[1].split("\n")[0]


def test_to_prompt_renders_chat_type() -> None:
    ctx = MessageContext(
        platform="slack",
        channel_id="C123",
        channel_name="#general",
        user_id="U04ABC123",
        user_name="Gavin Vickery",
        username="gavin",
        chat_type="thread",
    )
    result = ctx.to_prompt()
    assert "- Chat type: thread" in result


def test_slack_message_formatting_uses_utc() -> None:
    transport = SlackTransport(
        name="slack",
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
