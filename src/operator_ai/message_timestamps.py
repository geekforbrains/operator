from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from operator_ai.config import Config

MESSAGE_CREATED_AT_KEY = "_operator_created_at"


def attach_message_created_at(
    message: dict[str, Any],
    *,
    created_at: datetime | str | None = None,
) -> dict[str, Any]:
    stamped = dict(message)
    value = _normalize_created_at(created_at)
    if value is not None:
        stamped[MESSAGE_CREATED_AT_KEY] = value
    return stamped


def build_message_timestamp_prefix(
    config: Config,
    *,
    created_at: datetime | str,
) -> str:
    current = _parse_created_at(created_at)
    if current is None:
        return ""
    local = current.astimezone(config.tz).replace(microsecond=0)
    return f"[{local.strftime('%A')}, {local.isoformat(timespec='seconds')}]"


def utc_now_iso(now: datetime | None = None) -> str:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def _normalize_created_at(created_at: datetime | str | None) -> str | None:
    if created_at is None:
        return utc_now_iso()
    if isinstance(created_at, datetime):
        current = (
            created_at.astimezone(UTC) if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        )
        return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(created_at, str):
        value = created_at.strip()
        return value or None
    return None


def _parse_created_at(created_at: datetime | str) -> datetime | None:
    if isinstance(created_at, datetime):
        current = created_at
    elif isinstance(created_at, str):
        value = created_at.strip()
        if not value:
            return None
        try:
            current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


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
