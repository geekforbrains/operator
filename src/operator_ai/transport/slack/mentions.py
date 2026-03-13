from __future__ import annotations

import re

from operator_ai.transport.slack.config import (
    CHANNEL_MENTION_RE,
    USER_MENTION_RE,
    SlackUserProfile,
)

_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")


def build_user_mention_patterns(
    directory: dict[str, SlackUserProfile],
) -> list[tuple[re.Pattern[str], str]]:
    """Build sorted (longest-first) unique display-name → <@UID> patterns."""
    by_name: dict[str, list[str]] = {}
    for profile in directory.values():
        if profile.is_deleted:
            continue
        by_name.setdefault(profile.display_name.lower(), []).append(profile.user_id)
    return [
        (
            re.compile(r"(?<![<\w])@" + re.escape(name) + r"\b", re.IGNORECASE),
            f"<@{uids[0]}>",
        )
        for name, uids in sorted(by_name.items(), key=lambda x: len(x[0]), reverse=True)
        if len(uids) == 1
    ]


def build_channel_mention_patterns(
    channel_ids: dict[str, str],
) -> list[tuple[re.Pattern[str], str]]:
    """Build #channel-name → <#CID> patterns."""
    return [
        (
            re.compile(r"(?<!<)#" + re.escape(ch_name) + r"\b", re.IGNORECASE),
            f"<#{ch_id}>",
        )
        for ch_name, ch_id in channel_ids.items()
    ]


def expand_mentions(
    text: str,
    user_patterns: list[tuple[re.Pattern[str], str]],
    channel_patterns: list[tuple[re.Pattern[str], str]],
) -> str:
    """Resolve @Name and #channel to Slack link syntax, skipping code blocks."""
    if not user_patterns and not channel_patterns:
        return text
    parts = _CODE_BLOCK_RE.split(text)
    for i, part in enumerate(parts):
        if _CODE_BLOCK_RE.fullmatch(part):
            continue
        for pattern, replacement in user_patterns:
            part = pattern.sub(replacement, part)
        for pattern, replacement in channel_patterns:
            part = pattern.sub(replacement, part)
        parts[i] = part
    return "".join(parts)


def format_channel_reference(channel_id: str, label: str) -> str:
    name = label.strip()
    if name and name != "private channel":
        if not name.startswith("#"):
            name = f"#{name}"
        return f"<#{channel_id}> ({name})"
    return f"<#{channel_id}>"


async def render_slack_text(text: str, resolve_user: callable) -> str:
    """Resolve Slack mention markup to human-readable form."""
    text = CHANNEL_MENTION_RE.sub(
        lambda match: format_channel_reference(match.group(1), match.group(2)),
        text,
    )
    mention_ids = list(dict.fromkeys(USER_MENTION_RE.findall(text)))
    if mention_ids:
        replacements: dict[str, str] = {}
        for user_id in mention_ids:
            name = await resolve_user(user_id)
            replacements[user_id] = f"<@{user_id}> ({name})"

        def _replace(match: re.Match[str]) -> str:
            uid = match.group(1)
            return replacements.get(uid, f"@{uid}")

        text = USER_MENTION_RE.sub(_replace, text)
    return text.strip()
