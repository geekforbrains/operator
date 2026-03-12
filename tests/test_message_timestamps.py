from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from operator_ai.message_timestamps import (
    build_message_timestamp_prefix,
    format_ts,
)

VANCOUVER = ZoneInfo("America/Vancouver")

# 2026-03-06 17:45:00 UTC as unix timestamp
TS_2026_03_06_1745 = datetime(2026, 3, 6, 17, 45, tzinfo=UTC).timestamp()

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


def test_format_ts_zero_returns_never() -> None:
    assert format_ts(0.0) == "never"


def test_format_ts_formats_utc() -> None:
    assert format_ts(TS_2026_03_06_1745) == "2026-03-06T17:45:00Z"


def test_format_ts_formats_with_timezone() -> None:
    result = format_ts(TS_2026_03_06_1745, tz=VANCOUVER)
    assert result == "2026-03-06T09:45:00-08:00"
