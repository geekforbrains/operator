from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from operator_ai.config import Config
from operator_ai.prompts import CACHE_BOUNDARY

CURRENT_TIME_HEADER = "# Current Time"


def build_current_time_block(
    config: Config,
    *,
    now: datetime | None = None,
) -> str:
    current = (now or datetime.now(config.tz)).astimezone(config.tz).replace(microsecond=0)
    offset = _format_utc_offset(current.utcoffset())
    timezone_label = current.tzname() or config.runtime.timezone
    return "\n".join(
        [
            CURRENT_TIME_HEADER,
            "",
            "This timestamp was injected by Operator for this request only.",
            f"- Current local time: {current.strftime('%Y-%m-%d %H:%M:%S')} {timezone_label}",
            f"- Current weekday: {current.strftime('%A')}",
            f"- Timezone: {config.runtime.timezone} ({offset})",
            f"- ISO timestamp: {current.isoformat()}",
        ]
    )


def inject_current_time(
    messages: list[dict[str, Any]],
    config: Config,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not messages:
        return messages

    request_messages = list(messages)
    time_block = build_current_time_block(config, now=now)

    for idx, message in enumerate(request_messages):
        if message.get("role") != "system":
            break

        content = message.get("content")
        if not isinstance(content, str):
            continue

        updated = dict(message)
        updated["content"] = _append_dynamic_system_section(content, time_block)
        request_messages[idx] = updated
        return request_messages

    return [{"role": "system", "content": time_block}, *request_messages]


def _append_dynamic_system_section(system_prompt: str, dynamic_section: str) -> str:
    if CACHE_BOUNDARY in system_prompt:
        stable, dynamic = system_prompt.split(CACHE_BOUNDARY, 1)
        dynamic_parts = [part.strip() for part in (dynamic, dynamic_section) if part.strip()]
        return stable.rstrip() + CACHE_BOUNDARY + "\n\n".join(dynamic_parts)
    return system_prompt.rstrip() + CACHE_BOUNDARY + dynamic_section.strip()


def _format_utc_offset(offset: timedelta | None) -> str:
    if offset is None:
        return "UTC+00:00"

    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
