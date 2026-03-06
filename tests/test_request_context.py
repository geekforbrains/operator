from __future__ import annotations

from datetime import UTC, datetime

from operator_ai.config import Config
from operator_ai.prompts import CACHE_BOUNDARY
from operator_ai.request_context import (
    CURRENT_TIME_HEADER,
    build_current_time_block,
    inject_current_time,
)


def _config(timezone: str = "America/Vancouver") -> Config:
    return Config(
        runtime={"timezone": timezone},
        defaults={"models": ["openai/gpt-4.1"]},
        agents={"operator": {}},
    )


def test_build_current_time_block_uses_configured_timezone() -> None:
    config = _config()

    block = build_current_time_block(
        config,
        now=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
    )

    assert CURRENT_TIME_HEADER in block
    assert "- Current local time: 2026-03-06 09:45:00 PST" in block
    assert "- Current weekday: Friday" in block
    assert "- Timezone: America/Vancouver (UTC-08:00)" in block
    assert "- ISO timestamp: 2026-03-06T09:45:00-08:00" in block


def test_inject_current_time_keeps_persisted_messages_clean() -> None:
    config = _config()
    messages = [
        {
            "role": "system",
            "content": "# System" + CACHE_BOUNDARY + "# Context\n\nExisting dynamic section",
        },
        {"role": "user", "content": "What time is it?"},
    ]

    request_messages = inject_current_time(
        messages,
        config,
        now=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
    )

    assert messages[0]["content"] == (
        "# System" + CACHE_BOUNDARY + "# Context\n\nExisting dynamic section"
    )

    request_system = request_messages[0]["content"]
    assert isinstance(request_system, str)
    stable, dynamic = request_system.split(CACHE_BOUNDARY, 1)
    assert stable == "# System"
    assert "# Context\n\nExisting dynamic section" in dynamic
    assert CURRENT_TIME_HEADER in dynamic
    assert dynamic.index("# Context") < dynamic.index(CURRENT_TIME_HEADER)
