from __future__ import annotations

import operator_ai.tools  # noqa: F401  — warm up circular imports
from operator_ai.transport.base import MessageContext


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
