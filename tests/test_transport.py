from __future__ import annotations

import asyncio

import operator_ai.tools  # noqa: F401  — warm up circular imports
from operator_ai.transport.slack import SlackTransport


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
