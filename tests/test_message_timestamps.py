from __future__ import annotations

from datetime import UTC, datetime

from operator_ai.config import Config
from operator_ai.message_timestamps import (
    MESSAGE_CREATED_AT_KEY,
    attach_message_created_at,
    build_message_timestamp_prefix,
    render_message_timestamps,
)


def _config(timezone: str = "America/Vancouver") -> Config:
    return Config(
        runtime={"timezone": timezone},
        defaults={"models": ["openai/gpt-4.1"]},
        agents={"operator": {}},
    )


def test_build_message_timestamp_prefix_uses_configured_timezone() -> None:
    prefix = build_message_timestamp_prefix(
        _config(),
        created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
    )

    assert prefix == "[Friday, 2026-03-06T09:45:00-08:00]"


def test_render_message_timestamps_stamps_user_messages_only() -> None:
    messages = [
        {"role": "system", "content": "# System"},
        attach_message_created_at(
            {"role": "user", "content": "What time is it?"},
            created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
        ),
        {"role": "assistant", "content": "Working on it."},
    ]

    rendered = render_message_timestamps(messages, _config())

    assert messages[1]["content"] == "What time is it?"
    assert MESSAGE_CREATED_AT_KEY in messages[1]
    assert rendered == [
        {"role": "system", "content": "# System"},
        {
            "role": "user",
            "content": "[Friday, 2026-03-06T09:45:00-08:00]\nWhat time is it?",
        },
        {"role": "assistant", "content": "Working on it."},
    ]


def test_render_message_timestamps_adds_text_block_for_attachment_only_user_message() -> None:
    messages = [
        attach_message_created_at(
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
                ],
            },
            created_at="2026-03-09T15:29:41Z",
        )
    ]

    rendered = render_message_timestamps(messages, _config())

    assert rendered == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[Monday, 2026-03-09T08:29:41-07:00]"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]
