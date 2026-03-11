from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
from markdown_to_mrkdwn import SlackMarkdownConverter
from pydantic import BaseModel, ConfigDict
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from typing_extensions import override

from operator_ai.store import Store
from operator_ai.tools.registry import ToolDef
from operator_ai.transport.base import Attachment, IncomingMessage, MessageContext, Transport
from operator_ai.transport.registry import SetupSecret, SetupTransport, TransportDefinition

logger = logging.getLogger("operator.transport.slack")

LEADING_BOT_MENTION_RE = re.compile(r"^<@[A-Z0-9]+>\s*")
USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
CHANNEL_MENTION_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]+)>")
_mrkdwn = SlackMarkdownConverter()

MAX_API_ATTEMPTS = 3
BASE_RETRY_SECONDS = 1.0

_SLACK_USER_ID_RE = re.compile(r"^(?:slack:)?(?:<@)?([UW][A-Z0-9]+)(?:\|[^>]+)?>?$", re.I)


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


def _extract_attachments(event: dict) -> list[Attachment]:
    """Extract file attachments from a Slack event."""
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


def _slack_ts_to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    return {
        normalized.bot_token,
        normalized.app_token,
    }


def _resolve_env_var(env_var: str, agent_name: str) -> str:
    value = os.environ.get(env_var)
    if not value:
        raise ValueError(f"Agent '{agent_name}' transport: env var '{env_var}' not set")
    return value


def _normalize_slack_identity(value: str) -> str:
    match = _SLACK_USER_ID_RE.match(value.strip())
    if not match:
        raise ValueError("Use a Slack user ID like U123ABC45.")
    return match.group(1).upper()


class SlackTransport(Transport):
    def __init__(
        self,
        name: str,
        agent_name: str,
        bot_token: str,
        app_token: str,
        *,
        store: Store | None = None,
        include_archived_channels: bool = False,
        inject_channels_into_prompt: bool = True,
        inject_users_into_prompt: bool = True,
        expand_mentions: bool = True,
    ):
        self.name = name
        self.platform = "slack"
        self.agent_name = agent_name
        self._bot_token = bot_token
        self._app_token = app_token
        self._store = store
        self._include_archived_channels = include_archived_channels
        self._inject_channels_into_prompt = inject_channels_into_prompt
        self._inject_users_into_prompt = inject_users_into_prompt
        self._expand_mentions = expand_mentions
        self._app: AsyncApp | None = None
        self._handler: AsyncSocketModeHandler | None = None
        self._background_tasks: set[asyncio.Task] = set()

        # In-memory caches (bulk-loaded on startup, updated by Slack events).
        self._users: dict[str, str] = {}
        self._user_directory: dict[str, SlackUserProfile] = {}
        self._channels: dict[str, str] = {}
        self._channel_ids: dict[str, str] = {}
        self._channel_info: dict[str, str] = {}

    @override
    async def start(self, on_message: Callable[[IncomingMessage], Awaitable[None]]) -> None:
        self._app = AsyncApp(token=self._bot_token)

        @self._app.event("app_mention")
        async def handle_mention(event: dict, say):  # noqa: ARG001
            # Skip DMs — the message handler below already covers them.
            # Without this guard, a DM @mention fires both events and
            # can cause duplicate processing.
            if event.get("channel_type") == "im":
                return
            self._create_task(self._dispatch(event, on_message, strip_leading_mention=True))

        @self._app.event("message")
        async def handle_message(event: dict, say):  # noqa: ARG001
            # Only handle DMs (im channels) — app_mention covers channels
            if event.get("channel_type") == "im" and not event.get("bot_id"):
                self._create_task(self._dispatch(event, on_message))

        @self._app.event("team_join")
        async def handle_team_join(event: dict, say):  # noqa: ARG001
            self._upsert_user(event.get("user", {}))

        @self._app.event("user_change")
        async def handle_user_change(event: dict, say):  # noqa: ARG001
            self._upsert_user(event.get("user", {}))

        @self._app.event("channel_created")
        async def handle_channel_created(event: dict, say):  # noqa: ARG001
            ch = event.get("channel", {})
            self._upsert_channel(ch.get("id", ""), ch.get("name", ""))

        @self._app.event("channel_rename")
        async def handle_channel_rename(event: dict, say):  # noqa: ARG001
            ch = event.get("channel", {})
            self._upsert_channel(ch.get("id", ""), ch.get("name", ""))

        @self._app.event("channel_archive")
        async def handle_channel_archive(event: dict, say):  # noqa: ARG001
            channel_id = event.get("channel", "")
            if channel_id and not self._include_archived_channels:
                self._remove_channel(channel_id)

        @self._app.event("channel_unarchive")
        async def handle_channel_unarchive(event: dict, say):  # noqa: ARG001
            channel_id = event.get("channel", "")
            if channel_id:
                await self._fetch_and_upsert_channel(channel_id)

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        try:
            await self._fetch_all_users()
            logger.debug("Loaded %d Slack users", len(self._user_directory))
        except Exception:
            logger.warning("Failed to load Slack users on startup", exc_info=True)
        try:
            await self._fetch_all_channels()
            logger.debug("Loaded %d Slack channels", len(self._channels))
        except Exception:
            logger.warning("Failed to load Slack channels on startup", exc_info=True)
        logger.info("Starting Slack transport '%s'", self.name)
        await self._handler.start_async()

    @override
    async def stop(self) -> None:
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._handler:
            await self._handler.close_async()
            logger.info("Stopped Slack transport '%s'", self.name)
            self._handler = None
        self._app = None

    def _create_task(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _require_app(self) -> AsyncApp:
        if self._app is None:
            raise RuntimeError("Transport not started")
        return self._app

    async def _api_call(self, operation: str, call: Callable[[], Awaitable[dict]]) -> dict:
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

    # --- Cache management ---

    def _upsert_user(self, raw_user: dict) -> None:
        user_id = raw_user.get("id", "")
        if not user_id:
            return
        profile = raw_user.get("profile", {}) or {}
        display_name = (
            profile.get("display_name")
            or profile.get("display_name_normalized")
            or raw_user.get("real_name")
            or profile.get("real_name")
            or raw_user.get("name")
            or user_id
        )
        self._users[user_id] = display_name
        self._user_directory[user_id] = SlackUserProfile(
            user_id=user_id,
            slack_name=raw_user.get("name", ""),
            display_name=display_name,
            real_name=raw_user.get("real_name") or profile.get("real_name", ""),
            email=profile.get("email", ""),
            is_bot=bool(raw_user.get("is_bot") or raw_user.get("is_app_user")),
            is_deleted=bool(raw_user.get("deleted")),
        )

    def _upsert_channel(
        self, channel_id: str, name: str, topic: str = "", purpose: str = ""
    ) -> None:
        if not channel_id or not name:
            return
        ch_name = f"#{name}" if not name.startswith("#") else name
        self._channels[channel_id] = ch_name
        self._channel_ids[name.lstrip("#")] = channel_id
        snippet = topic or purpose
        if snippet:
            self._channel_info[channel_id] = snippet
        elif channel_id in self._channel_info:
            del self._channel_info[channel_id]

    def _remove_channel(self, channel_id: str) -> None:
        name = self._channels.pop(channel_id, "")
        if name:
            self._channel_ids.pop(name.lstrip("#"), None)
        self._channel_info.pop(channel_id, None)

    async def _fetch_and_upsert_channel(self, channel_id: str) -> None:
        app = self._require_app()
        try:
            resp = await self._api_call(
                "conversations.info",
                lambda: app.client.conversations_info(channel=channel_id),
            )
            ch = resp.get("channel", {})
            name = ch.get("name", "")
            topic = ch.get("topic", {}).get("value", "")
            purpose = ch.get("purpose", {}).get("value", "")
            if name:
                self._upsert_channel(channel_id, name, topic, purpose)
        except Exception:
            logger.warning("Failed to fetch channel info for %s", channel_id)

    async def _fetch_all_channels(self) -> None:
        app = self._require_app()
        channels: dict[str, str] = {}
        channel_ids: dict[str, str] = {}
        channel_info: dict[str, str] = {}

        cursor = None
        while True:
            params: dict[str, object] = {
                "types": "public_channel,private_channel",
                "limit": 200,
                "exclude_archived": not self._include_archived_channels,
            }
            if cursor:
                params["cursor"] = cursor
            request_params = dict(params)
            resp = await self._api_call(
                "conversations.list",
                lambda rp=request_params: app.client.conversations_list(**rp),
            )
            for ch in resp.get("channels", []):
                if not self._include_archived_channels and ch.get("is_archived"):
                    continue
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

        self._channels = channels
        self._channel_ids = channel_ids
        self._channel_info = channel_info

    async def _fetch_all_users(self) -> None:
        """Paginate users.list and replace user caches."""
        app = self._require_app()
        self._users.clear()
        self._user_directory.clear()

        cursor = None
        while True:
            params: dict[str, object] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            request_params = dict(params)
            resp = await self._api_call(
                "users.list",
                lambda rp=request_params: app.client.users_list(**rp),
            )
            for raw_user in resp.get("members", []):
                self._upsert_user(raw_user)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

    # --- Outbound mention resolution ---

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")

    def _resolve_outbound_mentions(self, text: str) -> str:
        """Best-effort resolve @Name and #channel to Slack link syntax.

        Skips content inside inline code and fenced code blocks.
        """
        if not self._expand_mentions:
            return text
        if not self._user_directory and not self._channel_ids:
            return text

        # Split text into code vs non-code segments
        parts = self._CODE_BLOCK_RE.split(text)
        for i, part in enumerate(parts):
            if self._CODE_BLOCK_RE.fullmatch(part):
                continue
            parts[i] = self._expand_mentions_in(part)
        return "".join(parts)

    def _expand_mentions_in(self, text: str) -> str:
        if self._user_directory:
            by_name: dict[str, str] = {}
            for profile in self._user_directory.values():
                if profile.is_deleted:
                    continue
                by_name[profile.display_name.lower()] = profile.user_id
            for name in sorted(by_name, key=len, reverse=True):
                uid = by_name[name]
                pattern = re.compile(r"(?<![<\w])@" + re.escape(name) + r"\b", re.IGNORECASE)
                text = pattern.sub(f"<@{uid}>", text)

        if self._channel_ids:
            for ch_name, ch_id in self._channel_ids.items():
                pattern = re.compile(r"(?<!<)#" + re.escape(ch_name) + r"\b", re.IGNORECASE)
                text = pattern.sub(f"<#{ch_id}>", text)

        return text

    # --- Messaging ---

    @override
    async def send(self, channel_id: str, text: str, thread_id: str | None = None) -> str:
        app = self._require_app()
        text = self._resolve_outbound_mentions(text)
        kwargs = {"channel": channel_id, "text": _mrkdwn.convert(text)}
        if thread_id:
            kwargs["thread_ts"] = thread_id
        resp = await self._api_call(
            "chat.postMessage",
            lambda: app.client.chat_postMessage(**kwargs),
        )
        return resp["ts"]

    @override
    async def update(
        self, channel_id: str, message_id: str, text: str, thread_id: str | None = None
    ) -> None:
        app = self._require_app()
        text = self._resolve_outbound_mentions(text)
        await self._api_call(
            "chat.update",
            lambda: app.client.chat_update(
                channel=channel_id, ts=message_id, text=_mrkdwn.convert(text)
            ),
        )

    @override
    async def delete(self, channel_id: str, message_id: str, thread_id: str | None = None) -> None:
        app = self._require_app()
        await self._api_call(
            "chat.delete",
            lambda: app.client.chat_delete(channel=channel_id, ts=message_id),
        )

    # --- File handling ---

    _MAX_DOWNLOAD = 50 * 1024 * 1024  # 50 MB hard limit on file downloads

    @override
    async def download_file(self, attachment: Attachment) -> bytes:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {self._bot_token}"}
            async with session.get(attachment.url, headers=headers) as resp:
                resp.raise_for_status()
                length = resp.content_length
                if length is not None and length > self._MAX_DOWNLOAD:
                    raise ValueError(f"File too large: {length} bytes (limit {self._MAX_DOWNLOAD})")
                if length is not None:
                    # Content-Length known and within limits — safe to read at once
                    return await resp.read()
                # Unknown size — read incrementally with a cap
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    total += len(chunk)
                    if total > self._MAX_DOWNLOAD:
                        raise ValueError(
                            f"File too large: >{self._MAX_DOWNLOAD} bytes (limit {self._MAX_DOWNLOAD})"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)

    @override
    async def send_file(
        self,
        channel_id: str,
        file_data: bytes,
        filename: str,
        thread_id: str | None = None,
    ) -> str:
        app = self._require_app()
        kwargs: dict = {
            "channel": channel_id,
            "content": file_data,
            "filename": filename,
        }
        if thread_id:
            kwargs["thread_ts"] = thread_id
        resp = await self._api_call(
            "files.upload_v2",
            lambda: app.client.files_upload_v2(**kwargs),
        )
        # files_upload_v2 returns file info; extract the message ts if available
        file_info = resp.get("file", {})
        shares = file_info.get("shares", {})
        for share_type in ("public", "private"):
            for channel_shares in shares.get(share_type, {}).values():
                if channel_shares:
                    return channel_shares[0].get("ts", "")
        return ""

    # --- Context resolution ---

    @override
    async def resolve_context(self, msg: IncomingMessage) -> MessageContext:
        # Strip transport prefix for Slack API calls; keep prefixed ID in context
        raw_user_id = msg.user_id.removeprefix("slack:")
        ch = msg.channel_id
        if ch.startswith("D"):
            chat_type = "dm"
        elif ch.startswith("C"):
            chat_type = "channel"
        elif ch.startswith("G"):
            chat_type = "group"
        else:
            chat_type = ""
        return MessageContext(
            platform="slack",
            channel_id=msg.channel_id,
            channel_name=await self._resolve_channel(msg.channel_id),
            user_id=msg.user_id,
            user_name=await self._resolve_user(raw_user_id),
            chat_type=chat_type,
        )

    async def _resolve_user(self, user_id: str) -> str:
        profile = self._user_directory.get(user_id)
        if profile:
            return profile.display_name

        cached = self._users.get(user_id)
        if cached:
            return cached

        app = self._require_app()
        try:
            resp = await self._api_call("users.info", lambda: app.client.users_info(user=user_id))
            user = resp.get("user", {})
            name = user.get("real_name") or user.get("profile", {}).get("display_name") or user_id
        except Exception:
            logger.warning("Failed to resolve Slack user %s, using raw ID", user_id)
            name = user_id

        self._users[user_id] = name
        return name

    def _linked_operator_usernames(self) -> dict[str, str]:
        if self._store is None:
            return {}
        linked: dict[str, str] = {}
        for user in self._store.list_users():
            for identity in user.identities:
                if identity.startswith("slack:"):
                    linked[identity.removeprefix("slack:")] = user.username
        return linked

    async def _format_user_reference(self, user_id: str) -> str:
        name = await self._resolve_user(user_id)
        return f"<@{user_id}> ({name})"

    def _format_channel_reference(self, channel_id: str, label: str) -> str:
        name = label.strip()
        if name and name != "private channel":
            if not name.startswith("#"):
                name = f"#{name}"
            return f"<#{channel_id}> ({name})"
        return f"<#{channel_id}>"

    async def _render_slack_text(self, text: str, *, strip_leading_mention: bool = False) -> str:
        if strip_leading_mention:
            text = LEADING_BOT_MENTION_RE.sub("", text, count=1)

        text = CHANNEL_MENTION_RE.sub(
            lambda match: self._format_channel_reference(match.group(1), match.group(2)),
            text,
        )

        mention_ids = list(dict.fromkeys(USER_MENTION_RE.findall(text)))
        if mention_ids:
            replacements: dict[str, str] = {}
            for user_id in mention_ids:
                replacements[user_id] = await self._format_user_reference(user_id)

            def _replace(match: re.Match[str]) -> str:
                user_id = match.group(1)
                return replacements.get(user_id, f"@{user_id}")

            text = USER_MENTION_RE.sub(_replace, text)
        return text.strip()

    async def _resolve_channel(self, channel_id: str) -> str:
        cached = self._channels.get(channel_id)
        if cached:
            return cached

        # D = DM, C = channel, G = group
        if channel_id.startswith("D"):
            name = "DM"
        else:
            app = self._require_app()
            try:
                resp = await self._api_call(
                    "conversations.info",
                    lambda: app.client.conversations_info(channel=channel_id),
                )
                channel = resp.get("channel", {})
                name = channel.get("name") or channel_id
                if not name.startswith("#"):
                    name = f"#{name}"
            except Exception:
                logger.warning("Failed to resolve Slack channel %s, using raw ID", channel_id)
                name = channel_id

        self._channels[channel_id] = name
        return name

    @override
    async def resolve_channel_id(self, channel: str) -> str | None:
        if channel.startswith(("C", "G", "D")) and len(channel) > 1:
            return channel
        name = channel.lstrip("#")
        return self._channel_ids.get(name)

    # --- Transport-scoped tools ---

    def _format_channel_list(self, query: str = "") -> list[str]:
        """Format the cached channel list as markdown bullet lines."""
        lines: list[str] = []
        query_text = query.strip().casefold()
        for ch_id, ch_name in sorted(self._channels.items(), key=lambda x: x[1]):
            info = self._channel_info.get(ch_id, "")
            haystack = f"{ch_name} {ch_id} {info}".casefold()
            if query_text and query_text not in haystack:
                continue
            suffix = f" — {info}" if info else ""
            lines.append(f"- <#{ch_id}> {ch_name}{suffix}")
        return lines

    @override
    def get_tools(self) -> list[ToolDef]:
        async def find_slack_users(
            query: str = "", limit: int = 20, linked_only: bool = False
        ) -> str:
            """Find Slack users by name, Slack ID, or linked Operator username.

            Args:
                query: Optional search text matched against Slack display name, real name, handle, Slack ID, email, or linked Operator username.
                limit: Maximum number of results to return (1-50, default 20).
                linked_only: When true, only return Slack users linked to an Operator user.
            """
            query_text = query.strip().casefold()
            limit = max(1, min(limit, 50))
            operator_usernames = self._linked_operator_usernames()
            matches: list[tuple[SlackUserProfile, str]] = []
            for profile in self._user_directory.values():
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
                if query.strip():
                    return "No matching Slack users found."
                return "No Slack users available."

            matches.sort(
                key=lambda item: (
                    item[1] == "",
                    item[0].display_name.casefold(),
                    item[0].user_id,
                )
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
                lines.append(
                    f"...and {len(matches) - limit} more. Refine `query` to narrow results."
                )
            return "\n".join(lines)

        async def list_channels(query: str = "") -> str:
            """List available Slack channels the bot can post to."""
            lines = self._format_channel_list(query=query)
            if lines:
                return "\n".join(lines)
            if query.strip():
                return "No matching channels found."
            return "No channels available."

        async def read_channel(channel: str, count: int = 20) -> str:
            """Read recent messages from a Slack channel.

            Args:
                channel: Channel name (e.g. #general) or channel ID.
                count: Number of recent messages to fetch (max 100, default 20).
            """
            channel_id = await self.resolve_channel_id(channel)
            if channel_id is None:
                return f"[error: could not resolve channel '{channel}']"
            app = self._require_app()
            count = max(1, min(count, 100))
            try:
                resp = await self._api_call(
                    "conversations.history",
                    lambda: app.client.conversations_history(channel=channel_id, limit=count),
                )
            except Exception as e:
                return f"[error: failed to read channel: {e}]"
            messages = resp.get("messages", [])
            if not messages:
                return "No messages found."
            return await self._format_messages(messages)

        async def read_thread(channel: str, thread_id: str, count: int = 50) -> str:
            """Read messages from a Slack thread.

            Args:
                channel: Channel name (e.g. #general) or channel ID where the thread lives.
                thread_id: The thread timestamp (ts) of the parent message.
                count: Number of messages to fetch (max 100, default 50).
            """
            channel_id = await self.resolve_channel_id(channel)
            if channel_id is None:
                return f"[error: could not resolve channel '{channel}']"
            app = self._require_app()
            count = max(1, min(count, 100))
            try:
                resp = await self._api_call(
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
            return await self._format_messages(messages)

        return [
            ToolDef(
                find_slack_users,
                "Find Slack users by display name, Slack ID, or linked Operator username.",
                status_label="Finding Slack users...",
            ),
            ToolDef(
                list_channels,
                "List available Slack channels the bot can post to, optionally filtering by name, ID, topic, or purpose.",
                status_label="Listing channels...",
            ),
            ToolDef(
                read_channel,
                "Read recent messages from a Slack channel. Use this to see what's been discussed.",
                status_label="Reading channel...",
            ),
            ToolDef(
                read_thread,
                "Read messages from a specific Slack thread. Use this to get full context on a conversation.",
                status_label="Reading thread...",
            ),
        ]

    @override
    def get_prompt_extra(self) -> str:
        lines = [
            "# Messaging",
            "",
            "Use `send_message` with a channel name (e.g. `#general`) or channel ID.",
            "It returns a Slack message timestamp you can pass as `thread_id` to reply in a thread.",
            "Use `list_channels` if you need to inspect Slack destinations first.",
            "Use `find_slack_users` to resolve people by name, Slack ID, or linked Operator username.",
            "When mentioning users or channels in messages, use `@Name` or `#channel`.",
            "Explicit `<@UID>` and `<#CID>` syntax also works.",
        ]
        if self._inject_users_into_prompt:
            if self._user_directory:
                lines += ["", "## Workspace Members", ""]
                visible = [p for p in self._user_directory.values() if not p.is_deleted]
                visible.sort(key=lambda p: p.display_name.casefold())
                for profile in visible:
                    suffix = " (bot)" if profile.is_bot else ""
                    lines.append(f"- {profile.display_name} <@{profile.user_id}>{suffix}")
            else:
                lines += [
                    "",
                    "User list not cached yet. Call `find_slack_users` if needed.",
                ]
        if self._inject_channels_into_prompt:
            if self._channels:
                lines += ["", "## Channels", ""]
                lines += self._format_channel_list()
            else:
                lines += ["", "Channel names are not cached yet. Call `list_channels` if needed."]
        return "\n".join(lines)

    # --- Message formatting ---

    async def _format_messages(self, messages: list[dict]) -> str:
        """Format a list of Slack messages into readable text."""
        # Slack returns newest-first for history, oldest-first for replies
        lines: list[str] = []
        for m in messages:
            user_id = m.get("user", "unknown")
            name = await self._resolve_user(user_id)
            try:
                ts = float(m.get("ts", "0"))
                dt = datetime.fromtimestamp(ts, tz=UTC)
                time_str = dt.strftime("%-I:%M %p")
            except (TypeError, ValueError):
                time_str = "unknown time"
            text = await self._render_slack_text(m.get("text", ""))
            # Note file attachments in formatted output
            files = m.get("files", [])
            if files:
                file_names = [f.get("name", "file") for f in files]
                text += f" [attached: {', '.join(file_names)}]"
            thread_ts = m.get("thread_ts")
            reply_count = m.get("reply_count", 0)
            suffix = f" (thread: {thread_ts}, {reply_count} replies)" if reply_count else ""
            lines.append(f"[{name}] {time_str}: {text}{suffix}")
        return "\n".join(lines)

    # --- Thread context ---

    @override
    async def get_thread_context(self, msg: IncomingMessage) -> str | None:
        app = self._require_app()
        try:
            resp = await self._api_call(
                "conversations.replies",
                lambda: app.client.conversations_replies(
                    channel=msg.channel_id, ts=msg.root_message_id
                ),
            )
        except Exception:
            logger.warning("Failed to fetch thread replies for %s", msg.root_message_id)
            return None

        replies = resp.get("messages", [])
        # Filter out the triggering message itself
        replies = [r for r in replies if r.get("ts") != msg.message_id]
        if not replies:
            return None

        total = len(replies)
        if total > 50:
            replies = replies[-50:]

        prefix = f"(showing last 50 of {total} messages)\n" if total > 50 else ""
        return prefix + await self._format_messages(replies)

    # --- Dispatch ---

    async def _dispatch(
        self,
        event: dict,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
        *,
        strip_leading_mention: bool = False,
    ) -> None:
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            # Ignore edited/deleted/system message variants,
            # but allow file_share (message with uploaded files).
            return
        text = await self._render_slack_text(
            event.get("text", ""),
            strip_leading_mention=strip_leading_mention,
        )

        # Extract file attachments
        attachments = _extract_attachments(event)

        if not text and not attachments:
            return

        channel_id = event.get("channel", "")
        message_id = event.get("ts", "")
        raw_user = event.get("user", "")
        if not channel_id or not message_id or not raw_user:
            logger.debug(
                "Skipping Slack event missing fields: channel=%s ts=%s user=%s",
                channel_id,
                message_id,
                raw_user,
            )
            return

        root_message_id = event.get("thread_ts") or message_id
        msg = IncomingMessage(
            text=text,
            user_id=f"slack:{raw_user}",
            channel_id=channel_id,
            message_id=message_id,
            root_message_id=root_message_id,
            transport_name=self.name,
            is_private=(event.get("channel_type") == "im"),
            was_mentioned=strip_leading_mention,
            attachments=attachments,
            created_at=_slack_ts_to_float(message_id),
        )
        await on_message(msg)


def create_slack_transport(
    name: str,
    agent_name: str,
    env: dict[str, Any],
    settings: dict[str, Any],
    store: Store,
) -> SlackTransport:
    normalized_env = SlackTransportEnv(**env)
    normalized_settings = SlackTransportSettings(**settings)
    return SlackTransport(
        name=name,
        agent_name=agent_name,
        bot_token=_resolve_env_var(normalized_env.bot_token, agent_name),
        app_token=_resolve_env_var(normalized_env.app_token, agent_name),
        store=store,
        include_archived_channels=normalized_settings.include_archived_channels,
        inject_channels_into_prompt=normalized_settings.inject_channels_into_prompt,
        inject_users_into_prompt=normalized_settings.inject_users_into_prompt,
        expand_mentions=normalized_settings.expand_mentions,
    )


SLACK_SETUP_TRANSPORT = SetupTransport(
    name="slack",
    label="Slack",
    description="Slack Socket Mode bot",
    identity_prompt="Your Slack user ID",
    identity_help="Paste your Slack user ID, for example U123ABC45.",
    secrets=(
        SetupSecret(
            env_vars=("SLACK_BOT_TOKEN",),
            prompt="Slack bot token (xoxb-*)",
            warning_prefix="xoxb-",
        ),
        SetupSecret(
            env_vars=("SLACK_APP_TOKEN",),
            prompt="Slack app token (xapp-*)",
            warning_prefix="xapp-",
        ),
    ),
    env_defaults={
        "bot_token": "SLACK_BOT_TOKEN",
        "app_token": "SLACK_APP_TOKEN",
    },
    settings_defaults={
        "include_archived_channels": False,
        "inject_channels_into_prompt": True,
        "inject_users_into_prompt": True,
        "expand_mentions": True,
    },
    run_hint="DM the Slack bot or @mention it in a channel where it is invited.",
    next_steps=(
        "Run [bold]operator[/bold] in a terminal (or [bold]operator service install[/bold]).",
        "DM your Slack bot or @mention it in an invited channel.",
        'Send a first message like [bold]"hello"[/bold].',
    ),
    normalize_identity=_normalize_slack_identity,
)


SLACK_TRANSPORT_DEFINITION = TransportDefinition(
    type_name="slack",
    create_transport=create_slack_transport,
    normalize_config=normalize_slack_transport_config,
    secret_env_vars=slack_secret_env_vars,
    logger_names=("slack_bolt", "slack_sdk"),
    setup=SLACK_SETUP_TRANSPORT,
)
