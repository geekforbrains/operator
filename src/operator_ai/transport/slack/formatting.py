"""Slack message formatting and prompt generation helpers.

Extracted from SlackTransport to keep the transport class focused on
lifecycle, dispatch, and API orchestration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from operator_ai.transport.slack.config import SlackUserProfile


async def format_slack_messages(
    messages: list[dict],
    *,
    resolve_user: Callable[[str], Awaitable[str]],
    render_text: Callable[[str], Awaitable[str]],
) -> str:
    """Format a list of Slack message dicts into readable text lines."""
    lines: list[str] = []
    for m in messages:
        user_id = m.get("user", "")
        if user_id:
            name = await resolve_user(user_id)
        elif m.get("bot_id"):
            name = m.get("username") or m.get("bot_id", "bot")
        else:
            name = "unknown"
        try:
            ts = float(m.get("ts", "0"))
            dt = datetime.fromtimestamp(ts, tz=UTC)
            time_str = dt.strftime("%I:%M %p").lstrip("0")
        except (TypeError, ValueError):
            time_str = "unknown time"
        text = await render_text(m.get("text", ""))
        files = m.get("files", [])
        if files:
            file_names = [f.get("name", "file") for f in files]
            text += f" [attached: {', '.join(file_names)}]"
        thread_ts = m.get("thread_ts")
        reply_count = m.get("reply_count", 0)
        suffix = f" (thread: {thread_ts}, {reply_count} replies)" if reply_count else ""
        lines.append(f"[{name}] {time_str}: {text}{suffix}")
    return "\n".join(lines)


def format_channel_list(
    channels: dict[str, str],
    channel_info: dict[str, str],
    query: str = "",
) -> list[str]:
    """Format channels into prompt-ready lines, optionally filtered by query."""
    lines: list[str] = []
    query_text = query.strip().casefold()
    for ch_id, ch_name in sorted(channels.items(), key=lambda x: x[1]):
        info = channel_info.get(ch_id, "")
        haystack = f"{ch_name} {ch_id} {info}".casefold()
        if query_text and query_text not in haystack:
            continue
        suffix = f" — {info}" if info else ""
        lines.append(f"- <#{ch_id}> {ch_name}{suffix}")
    return lines


def build_slack_prompt_extra(
    *,
    user_directory: dict[str, SlackUserProfile],
    channels: dict[str, str],
    channel_info: dict[str, str],
    inject_users: bool,
    inject_channels: bool,
) -> str:
    """Build the Slack-specific prompt extra block."""
    lines = [
        "# Messaging",
        "",
        "Slack sessions are thread-scoped.",
        "Every message addressed to you starts a new session thread or continues the current one.",
        "In channels, only messages that mention you are addressed to you; unmentioned thread chatter is ambient context unless you inspect it deliberately.",
        "Stay focused on the current thread unless you intentionally use Slack tools to inspect outside context.",
        "",
        "Use `send_message` with a channel name (e.g. `#general`) or channel ID.",
        "It returns a Slack message timestamp you can pass as `thread_id` to reply in a thread.",
        "Use `slack_read_channel` or `slack_read_thread` when you need context outside the current thread.",
        "Use `slack_list_channels` if you need to inspect Slack destinations first.",
        "Use `slack_find_users` to resolve people by name, Slack ID, or linked Operator username.",
        "Use `@Name` only when the display name is unambiguous; otherwise call `slack_find_users` and use the returned `<@UID>` mention.",
        "Use `#channel` for channels. Explicit `<@UID>` and `<#CID>` syntax also works.",
        "",
        "## Reactions",
        "",
        "Use `slack_add_reaction` and `slack_remove_reaction` to add/remove emoji reactions.",
        "Emoji names are without colons (e.g. `thumbsup`, `eyes`, `white_check_mark`).",
        "When others react to messages, you'll see those as context in your next interaction.",
    ]
    if inject_users:
        if user_directory:
            lines += ["", "## Workspace Members", ""]
            visible = [p for p in user_directory.values() if not p.is_deleted]
            visible.sort(key=lambda p: p.display_name.casefold())
            for profile in visible:
                suffix = " (bot)" if profile.is_bot else ""
                lines.append(f"- <@{profile.user_id}> {profile.display_name}{suffix}")
        else:
            lines += [
                "",
                "User list not cached yet. Call `slack_find_users` if needed.",
            ]
    if inject_channels:
        if channels:
            lines += ["", "## Channels", ""]
            lines += format_channel_list(channels, channel_info)
        else:
            lines += [
                "",
                "Channel names are not cached yet. Call `slack_list_channels` if needed.",
            ]
    return "\n".join(lines)
