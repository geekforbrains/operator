from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
CHANNEL_MENTION_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]+)>")

MAX_API_ATTEMPTS = 3
BASE_RETRY_SECONDS = 1.0


@dataclass(frozen=True)
class SlackUserProfile:
    user_id: str
    slack_name: str
    display_name: str
    real_name: str
    email: str
    is_bot: bool
    is_deleted: bool


class SlackTransportEnv(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: str
    app_token: str


class SlackTransportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_archived_channels: bool = False
    inject_channels_into_prompt: bool = True
    inject_users_into_prompt: bool = True
    expand_mentions: bool = True


def normalize_slack_transport_config(
    env: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        SlackTransportEnv(**env).model_dump(),
        SlackTransportSettings(**settings).model_dump(),
    )


def slack_secret_env_vars(env: dict[str, Any], _settings: dict[str, Any]) -> set[str]:
    normalized = SlackTransportEnv(**env)
    return {normalized.bot_token, normalized.app_token}


def resolve_env_var(env_var: str, agent_name: str) -> str:
    value = os.environ.get(env_var)
    if not value:
        raise ValueError(f"Agent '{agent_name}' transport: env var '{env_var}' not set")
    return value


def parse_user_profile(raw_user: dict) -> SlackUserProfile | None:
    user_id = raw_user.get("id", "")
    if not user_id:
        return None
    profile = raw_user.get("profile", {}) or {}
    display_name = (
        profile.get("display_name")
        or profile.get("display_name_normalized")
        or raw_user.get("real_name")
        or profile.get("real_name")
        or raw_user.get("name")
        or user_id
    )
    return SlackUserProfile(
        user_id=user_id,
        slack_name=raw_user.get("name", ""),
        display_name=display_name,
        real_name=raw_user.get("real_name") or profile.get("real_name", ""),
        email=profile.get("email", ""),
        is_bot=bool(raw_user.get("is_bot") or raw_user.get("is_app_user")),
        is_deleted=bool(raw_user.get("deleted")),
    )


def slack_ts_to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_attachments(event: dict) -> list:
    """Extract file attachments from a Slack event."""
    from operator_ai.transport.base import Attachment

    attachments: list[Attachment] = []
    for f in event.get("files", []):
        url = f.get("url_private", "")
        if not url:
            continue
        attachments.append(
            Attachment(
                filename=f.get("name", "unknown"),
                content_type=f.get("mimetype", "application/octet-stream"),
                size=f.get("size", 0),
                url=url,
                platform_id=f.get("id", ""),
            )
        )
    return attachments
