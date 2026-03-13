from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from slack_sdk.errors import SlackApiError

from operator_ai.transport.slack.config import SlackUserProfile, parse_user_profile

MAX_API_ATTEMPTS = 3
BASE_RETRY_SECONDS = 1.0

logger = logging.getLogger("operator.transport.slack")


async def api_call(operation: str, call: Callable[[], Awaitable[dict]]) -> dict:
    """Call Slack API with bounded retries for rate limit/transient failures."""
    for attempt in range(1, MAX_API_ATTEMPTS + 1):
        try:
            return await call()
        except SlackApiError as e:
            response = e.response
            status = getattr(response, "status_code", None)
            headers = getattr(response, "headers", {}) or {}
            if status == 429 and attempt < MAX_API_ATTEMPTS:
                retry_after = headers.get("Retry-After", "1")
                wait_seconds = max(float(retry_after), 1.0)
                logger.warning(
                    "Slack API rate-limited during %s (attempt %d/%d), retrying in %.1fs",
                    operation,
                    attempt,
                    MAX_API_ATTEMPTS,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue
            if status and status >= 500 and attempt < MAX_API_ATTEMPTS:
                wait_seconds = BASE_RETRY_SECONDS * attempt
                logger.warning(
                    "Slack API server error during %s (status=%s, attempt %d/%d), retrying in %.1fs",
                    operation,
                    status,
                    attempt,
                    MAX_API_ATTEMPTS,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue
            raise
        except (TimeoutError, OSError):
            if attempt == MAX_API_ATTEMPTS:
                raise
            wait_seconds = BASE_RETRY_SECONDS * attempt
            logger.warning(
                "Transient Slack client failure during %s (attempt %d/%d), retrying in %.1fs",
                operation,
                attempt,
                MAX_API_ATTEMPTS,
                wait_seconds,
                exc_info=True,
            )
            await asyncio.sleep(wait_seconds)
    raise RuntimeError(f"Slack API retries exhausted for {operation}")


async def fetch_all_users(
    client: Any,
) -> dict[str, SlackUserProfile]:
    """Paginate users.list and return full user directory."""
    directory: dict[str, SlackUserProfile] = {}
    cursor = None
    while True:
        params: dict[str, object] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        request_params = dict(params)
        resp = await api_call(
            "users.list",
            lambda rp=request_params: client.users_list(**rp),
        )
        for raw_user in resp.get("members", []):
            profile = parse_user_profile(raw_user)
            if profile:
                directory[profile.user_id] = profile
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return directory


async def fetch_all_channels(
    client: Any,
    *,
    include_archived: bool = False,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Paginate conversations.list and return (id->name, name->id, id->info) dicts."""
    channels: dict[str, str] = {}
    channel_ids: dict[str, str] = {}
    channel_info: dict[str, str] = {}

    cursor = None
    while True:
        params: dict[str, object] = {
            "types": "public_channel,private_channel",
            "limit": 200,
            "exclude_archived": not include_archived,
        }
        if cursor:
            params["cursor"] = cursor
        request_params = dict(params)
        resp = await api_call(
            "conversations.list",
            lambda rp=request_params: client.conversations_list(**rp),
        )
        for ch in resp.get("channels", []):
            ch_id = ch.get("id", "")
            ch_name = ch.get("name", "")
            if not ch_id or not ch_name:
                continue
            channels[ch_id] = f"#{ch_name}"
            channel_ids[ch_name] = ch_id
            topic = ch.get("topic", {}).get("value", "")
            purpose = ch.get("purpose", {}).get("value", "")
            snippet = topic or purpose
            if snippet:
                channel_info[ch_id] = snippet
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return channels, channel_ids, channel_info
