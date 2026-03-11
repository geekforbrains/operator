from __future__ import annotations

from datetime import UTC, datetime

from operator_ai.config import Config
from operator_ai.message_timestamps import build_message_timestamp_prefix


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
