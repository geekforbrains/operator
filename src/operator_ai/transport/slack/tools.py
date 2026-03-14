from __future__ import annotations

from typing import TYPE_CHECKING

from operator_ai.tools.registry import ToolDef
from operator_ai.transport.slack.api import api_call

if TYPE_CHECKING:
    from operator_ai.transport.slack.config import SlackUserProfile
    from operator_ai.transport.slack.transport import SlackTransport


def get_slack_tools(transport: SlackTransport) -> list[ToolDef]:
    """Build the agent-facing Slack tool definitions."""

    async def slack_find_users(query: str = "", limit: int = 20, linked_only: bool = False) -> str:
        """Find Slack users by name, Slack ID, or linked Operator username.

        Args:
            query: Optional search text matched against Slack display name, real name, handle, Slack ID, email, or linked Operator username.
            limit: Maximum number of results to return (1-50, default 20).
            linked_only: When true, only return Slack users linked to an Operator user.
        """
        query_text = query.strip().casefold()
        limit = max(1, min(limit, 50))
        operator_usernames = transport.linked_operator_usernames()
        matches: list[tuple[SlackUserProfile, str]] = []
        for profile in transport.user_directory.values():
            if profile.is_bot or profile.is_deleted:
                continue
            operator_username = operator_usernames.get(profile.user_id, "")
            if linked_only and not operator_username:
                continue
            haystack = " ".join(
                part
                for part in (
                    profile.user_id,
                    profile.display_name,
                    profile.real_name,
                    profile.slack_name,
                    profile.email,
                    operator_username,
                )
                if part
            ).casefold()
            if query_text and query_text not in haystack:
                continue
            matches.append((profile, operator_username))

        if not matches:
            return (
                "No matching Slack users found." if query.strip() else "No Slack users available."
            )

        matches.sort(
            key=lambda item: (item[1] == "", item[0].display_name.casefold(), item[0].user_id)
        )
        lines: list[str] = []
        for profile, operator_username in matches[:limit]:
            aliases: list[str] = []
            if profile.slack_name:
                aliases.append(f"@{profile.slack_name}")
            if profile.real_name and profile.real_name != profile.display_name:
                aliases.append(profile.real_name)
            alias_text = f" ({', '.join(aliases)})" if aliases else ""
            line = f"- {profile.display_name}{alias_text} — Mention `<@{profile.user_id}>`"
            if operator_username:
                line += f" — Operator `{operator_username}`"
            else:
                line += " — Operator [unlinked]"
            lines.append(line)
        if len(matches) > limit:
            lines.append(f"...and {len(matches) - limit} more. Refine `query` to narrow results.")
        return "\n".join(lines)

    async def slack_list_channels(query: str = "") -> str:
        """List available Slack channels the bot can post to."""
        lines = transport.format_channel_list(query=query)
        if lines:
            return "\n".join(lines)
        return "No matching channels found." if query.strip() else "No channels available."

    async def slack_read_channel(channel: str, count: int = 20) -> str:
        """Read recent messages from a Slack channel.

        Args:
            channel: Channel name (e.g. #general) or channel ID.
            count: Number of recent messages to fetch (max 100, default 20).
        """
        channel_id = await transport.resolve_channel_id(channel)
        if channel_id is None:
            return f"[error: could not resolve channel '{channel}']"
        app = transport.require_app()
        count = max(1, min(count, 100))
        try:
            resp = await api_call(
                "conversations.history",
                lambda: app.client.conversations_history(channel=channel_id, limit=count),
            )
        except Exception as e:
            return f"[error: failed to read channel: {e}]"
        messages = resp.get("messages", [])
        if not messages:
            return "No messages found."
        return await transport.format_messages(messages)

    async def slack_read_thread(channel: str, thread_id: str, count: int = 50) -> str:
        """Read messages from a Slack thread.

        Args:
            channel: Channel name (e.g. #general) or channel ID where the thread lives.
            thread_id: The thread timestamp (ts) of the parent message.
            count: Number of messages to fetch (max 100, default 50).
        """
        channel_id = await transport.resolve_channel_id(channel)
        if channel_id is None:
            return f"[error: could not resolve channel '{channel}']"
        app = transport.require_app()
        count = max(1, min(count, 100))
        try:
            resp = await api_call(
                "conversations.replies",
                lambda: app.client.conversations_replies(
                    channel=channel_id, ts=thread_id, limit=count
                ),
            )
        except Exception as e:
            return f"[error: failed to read thread: {e}]"
        messages = resp.get("messages", [])
        if not messages:
            return "No messages found in thread."
        return await transport.format_messages(messages)

    async def slack_add_reaction(channel: str, message_id: str, emoji: str) -> str:
        """Add an emoji reaction to a Slack message.

        Args:
            channel: Channel name (e.g. #general) or channel ID.
            message_id: The message timestamp (ts) to react to.
            emoji: Emoji name without colons (e.g. thumbsup, eyes, white_check_mark).
        """
        channel_id = await transport.resolve_channel_id(channel)
        if channel_id is None:
            return f"[error: could not resolve channel '{channel}']"
        app = transport.require_app()
        try:
            await api_call(
                "reactions.add",
                lambda: app.client.reactions_add(
                    channel=channel_id, timestamp=message_id, name=emoji
                ),
            )
            return f"Added :{emoji}: to message {message_id}"
        except Exception as e:
            return f"[error: failed to add reaction: {e}]"

    async def slack_remove_reaction(channel: str, message_id: str, emoji: str) -> str:
        """Remove an emoji reaction from a Slack message.

        Args:
            channel: Channel name (e.g. #general) or channel ID.
            message_id: The message timestamp (ts) to remove reaction from.
            emoji: Emoji name without colons (e.g. thumbsup, eyes, white_check_mark).
        """
        channel_id = await transport.resolve_channel_id(channel)
        if channel_id is None:
            return f"[error: could not resolve channel '{channel}']"
        app = transport.require_app()
        try:
            await api_call(
                "reactions.remove",
                lambda: app.client.reactions_remove(
                    channel=channel_id, timestamp=message_id, name=emoji
                ),
            )
            return f"Removed :{emoji}: from message {message_id}"
        except Exception as e:
            return f"[error: failed to remove reaction: {e}]"

    return [
        ToolDef(
            slack_find_users,
            "Find Slack users by display name, Slack ID, or linked Operator username.",
        ),
        ToolDef(
            slack_list_channels,
            "List available Slack channels the bot can post to, optionally filtering by name, ID, topic, or purpose.",
        ),
        ToolDef(
            slack_read_channel,
            "Read recent messages from a Slack channel. Use this to see what's been discussed.",
        ),
        ToolDef(
            slack_read_thread,
            "Read messages from a specific Slack thread. Use this to get full context on a conversation.",
        ),
        ToolDef(
            slack_add_reaction,
            "Add an emoji reaction to a Slack message. Use emoji name without colons.",
        ),
        ToolDef(
            slack_remove_reaction,
            "Remove an emoji reaction from a Slack message.",
        ),
    ]
