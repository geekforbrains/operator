from __future__ import annotations

import asyncio
from zoneinfo import ZoneInfo

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
    transport = SlackTransport(
        name="slack",
        agent_name="operator",
        bot_token="xoxb-test",
        app_token="xapp-test",
        tz=ZoneInfo("Asia/Tokyo"),
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

    assert "[Gavin] 9:00 PM: Hello" in formatted
