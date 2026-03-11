from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from operator_ai.message_timestamps import (
    MESSAGE_CREATED_AT_KEY,
    attach_message_created_at,
    build_message_timestamp_prefix,
    format_ts,
    render_message_timestamps,
)

VANCOUVER = ZoneInfo("America/Vancouver")

# 2026-03-06 17:45:00 UTC as unix timestamp
TS_2026_03_06_1745 = datetime(2026, 3, 6, 17, 45, tzinfo=UTC).timestamp()

# 2026-03-09 15:29:41 UTC as unix timestamp
TS_2026_03_09_1529 = datetime(2026, 3, 9, 15, 29, 41, tzinfo=UTC).timestamp()


def test_build_message_timestamp_prefix_uses_configured_timezone() -> None:
    prefix = build_message_timestamp_prefix(
        VANCOUVER,
        created_at=TS_2026_03_06_1745,
    )

    assert prefix == "[Friday, 2026-03-06T09:45:00-08:00]"


def test_build_message_timestamp_prefix_none_tz_uses_utc() -> None:
    prefix = build_message_timestamp_prefix(
        None,
        created_at=TS_2026_03_06_1745,
    )

    assert prefix == "[Friday, 2026-03-06T17:45:00+00:00]"


def test_render_message_timestamps_stamps_user_messages_only() -> None:
    messages = [
        {"role": "system", "content": "# System"},
        attach_message_created_at(
            {"role": "user", "content": "What time is it?"},
            created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
        ),
        {"role": "assistant", "content": "Working on it."},
    ]

    rendered = render_message_timestamps(messages, VANCOUVER)

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
            created_at=datetime(2026, 3, 9, 15, 29, 41, tzinfo=UTC),
        )
    ]

    rendered = render_message_timestamps(messages, VANCOUVER)

    assert rendered == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[Monday, 2026-03-09T08:29:41-07:00]"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]


def test_format_ts_zero_returns_never() -> None:
    assert format_ts(0.0) == "never"


def test_format_ts_formats_utc() -> None:
    assert format_ts(TS_2026_03_06_1745) == "2026-03-06T17:45:00Z"


def test_format_ts_formats_with_timezone() -> None:
    result = format_ts(TS_2026_03_06_1745, tz=VANCOUVER)
    assert result == "2026-03-06T09:45:00-08:00"
