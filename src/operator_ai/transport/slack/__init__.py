from __future__ import annotations

from typing import Any

from operator_ai.store import Store
from operator_ai.transport.registry import TransportDefinition
from operator_ai.transport.slack.config import (
    SlackTransportEnv,
    SlackTransportSettings,
    SlackUserProfile,
    extract_attachments,
    normalize_slack_transport_config,
    resolve_env_var,
    slack_secret_env_vars,
)
from operator_ai.transport.slack.transport import SlackTransport


def create_slack_transport(
    agent_name: str,
    env: dict[str, Any],
    settings: dict[str, Any],
    store: Store,
) -> SlackTransport:
    normalized_env = SlackTransportEnv(**env)
    normalized_settings = SlackTransportSettings(**settings)
    return SlackTransport(
        agent_name=agent_name,
        bot_token=resolve_env_var(normalized_env.bot_token, agent_name),
        app_token=resolve_env_var(normalized_env.app_token, agent_name),
        store=store,
        include_archived_channels=normalized_settings.include_archived_channels,
        inject_channels_into_prompt=normalized_settings.inject_channels_into_prompt,
        inject_users_into_prompt=normalized_settings.inject_users_into_prompt,
        expand_mentions=normalized_settings.expand_mentions,
    )


SLACK_TRANSPORT_DEFINITION = TransportDefinition(
    type_name="slack",
    create_transport=create_slack_transport,
    normalize_config=normalize_slack_transport_config,
    secret_env_vars=slack_secret_env_vars,
    logger_names=("slack_bolt", "slack_sdk"),
)

__all__ = [
    "SLACK_TRANSPORT_DEFINITION",
    "SlackTransport",
    "SlackUserProfile",
    "extract_attachments",
]
