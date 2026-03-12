from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

MESSAGE_CREATED_AT_KEY = "_operator_created_at"


def attach_message_created_at(
    message: dict[str, Any],
    *,
    created_at: datetime | float | None = None,
) -> dict[str, Any]:
    stamped = dict(message)
    stamped[MESSAGE_CREATED_AT_KEY] = _normalize_created_at(created_at)
    return stamped


def build_message_timestamp_prefix(
    tz: ZoneInfo | None,
    *,
    created_at: float,
) -> str:
    if not created_at:
        return ""
    if tz is None:
        tz = UTC
    local = datetime.fromtimestamp(created_at, tz=tz).replace(microsecond=0)
    return f"[{local.strftime('%A')}, {local.isoformat(timespec='seconds')}]"


def format_ts(ts: float, tz: ZoneInfo | None = None) -> str:
    """Format a unix timestamp for human display. Returns ISO 8601 string."""
    if not ts:
        return "never"
    dt = datetime.fromtimestamp(ts, tz=tz or UTC).replace(microsecond=0)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_created_at(created_at: datetime | float | None) -> float:
    if created_at is None:
        return time.time()
    if isinstance(created_at, (int, float)):
        return float(created_at)
    if isinstance(created_at, datetime):
        return created_at.timestamp()
    return time.time()


def _prefix_content(content: Any, prefix: str) -> Any:
    if not prefix:
        return content
    if isinstance(content, str):
        return _prefix_text(content, prefix)
    if isinstance(content, list):
        blocks = list(content)
        if blocks and isinstance(blocks[0], dict) and blocks[0].get("type") == "text":
            first = dict(blocks[0])
            first["text"] = _prefix_text(first.get("text"), prefix)
            blocks[0] = first
            return blocks
        return [{"type": "text", "text": prefix}, *blocks]
    if content is None:
        return prefix
    return _prefix_text(str(content), prefix)


def _prefix_text(text: Any, prefix: str) -> str:
    value = text if isinstance(text, str) else ""
    return f"{prefix}\n{value}" if value else prefix
