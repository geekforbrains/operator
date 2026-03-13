from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

import aiohttp
from markdown_to_mrkdwn import SlackMarkdownConverter
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from typing_extensions import override

from operator_ai.store import Store
from operator_ai.tools.registry import ToolDef
from operator_ai.transport.base import Attachment, IncomingMessage, MessageContext, Transport
from operator_ai.transport.slack.api import api_call, fetch_all_channels, fetch_all_users
from operator_ai.transport.slack.config import (
    SlackUserProfile,
    extract_attachments,
    parse_user_profile,
    slack_ts_to_float,
)
from operator_ai.transport.slack.formatting import (
    build_slack_prompt_extra,
    format_channel_list,
    format_slack_messages,
)
from operator_ai.transport.slack.mentions import (
    build_channel_mention_patterns,
    build_user_mention_patterns,
    expand_mentions,
    render_slack_text,
)
from operator_ai.transport.slack.tools import get_slack_tools

logger = logging.getLogger("operator.transport.slack")
_mrkdwn = SlackMarkdownConverter()


class SlackTransport(Transport):
    def __init__(
        self,
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

        self._bot_user_id: str = ""
        self._channel_refresh_handle: asyncio.TimerHandle | None = None

        # In-memory caches
        self.user_directory: dict[str, SlackUserProfile] = {}
        self._channels: dict[str, str] = {}
        self._channel_ids: dict[str, str] = {}
        self._channel_info: dict[str, str] = {}

        # Pre-compiled mention patterns, invalidated on cache changes
        self._user_mention_patterns: list[tuple[re.Pattern[str], str]] | None = None
        self._channel_mention_patterns: list[tuple[re.Pattern[str], str]] | None = None

        # Cached linked operator usernames (slack_id -> operator username)
        self._linked_operator_usernames: dict[str, str] | None = None

        # Shared HTTP session for file downloads
        self._http_session: aiohttp.ClientSession | None = None

    # --- Lifecycle ---

    @override
    async def start(self, on_message: Callable[[IncomingMessage], Awaitable[None]]) -> None:
        self._app = AsyncApp(token=self._bot_token)

        @self._app.event("app_mention")
        async def handle_mention(event: dict, say):  # noqa: ARG001
            if event.get("channel_type") == "im":
                return
            self._create_task(self._dispatch(event, on_message))

        @self._app.event("message")
        async def handle_message(event: dict, say):  # noqa: ARG001
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
            self._schedule_channel_refresh()

        @self._app.event("channel_rename")
        async def handle_channel_rename(event: dict, say):  # noqa: ARG001
            self._schedule_channel_refresh()

        @self._app.event("channel_archive")
        async def handle_channel_archive(event: dict, say):  # noqa: ARG001
            self._schedule_channel_refresh()

        @self._app.event("channel_unarchive")
        async def handle_channel_unarchive(event: dict, say):  # noqa: ARG001
            self._schedule_channel_refresh()

        @self._app.event("reaction_added")
        async def handle_reaction_added(event: dict, say):  # noqa: ARG001
            self._create_task(self._handle_reaction(event, added=True))

        @self._app.event("reaction_removed")
        async def handle_reaction_removed(event: dict, say):  # noqa: ARG001
            self._create_task(self._handle_reaction(event, added=False))

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        try:
            auth_resp = await api_call("auth.test", self._app.client.auth_test)
            self._bot_user_id = auth_resp.get("user_id", "")
            logger.debug("Bot user ID: %s", self._bot_user_id)
        except Exception:
            logger.warning("Failed to resolve bot user ID via auth.test", exc_info=True)
        try:
            self.user_directory = await fetch_all_users(self._app.client)
            logger.debug("Loaded %d Slack users", len(self.user_directory))
        except Exception:
            logger.warning("Failed to load Slack users on startup", exc_info=True)
        try:
            await self._refresh_channels()
            logger.debug("Loaded %d Slack channels", len(self._channels))
        except Exception:
            logger.warning("Failed to load Slack channels on startup", exc_info=True)
        logger.info("Starting Slack transport '%s'", self.agent_name)
        await self._handler.start_async()

    @override
    async def stop(self) -> None:
        if self._channel_refresh_handle is not None:
            self._channel_refresh_handle.cancel()
            self._channel_refresh_handle = None
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        if self._handler:
            await self._handler.close_async()
            logger.info("Stopped Slack transport '%s'", self.agent_name)
            self._handler = None
        self._app = None

    def _create_task(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()):
            logger.error("Background task failed", exc_info=exc)

    def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    # --- API helpers ---

    def require_app(self) -> AsyncApp:
        if self._app is None:
            raise RuntimeError("Transport not started")
        return self._app

    # --- Cache management ---

    _CHANNEL_REFRESH_DELAY = 2.0
    _MAX_THREAD_MESSAGES = 50

    def _schedule_channel_refresh(self) -> None:
        if self._channel_refresh_handle is not None:
            self._channel_refresh_handle.cancel()
        loop = asyncio.get_running_loop()
        self._channel_refresh_handle = loop.call_later(
            self._CHANNEL_REFRESH_DELAY,
            lambda: self._create_task(self._refresh_channels()),
        )

    async def _refresh_channels(self) -> None:
        app = self.require_app()
        channels, channel_ids, channel_info = await fetch_all_channels(
            app.client, include_archived=self._include_archived_channels
        )
        self._channels = channels
        self._channel_ids = channel_ids
        self._channel_info = channel_info
        self._channel_mention_patterns = None

    def _upsert_user(self, raw_user: dict) -> None:
        profile = parse_user_profile(raw_user)
        if profile:
            self.user_directory[profile.user_id] = profile
            self._user_mention_patterns = None

    def linked_operator_usernames(self) -> dict[str, str]:
        if self._linked_operator_usernames is not None:
            return self._linked_operator_usernames
        if self._store is None:
            return {}
        linked: dict[str, str] = {}
        for user in self._store.list_users():
            for identity in user.identities:
                if identity.startswith("slack:"):
                    linked[identity.removeprefix("slack:")] = user.username
        self._linked_operator_usernames = linked
        return linked

    def invalidate_linked_usernames(self) -> None:
        """Invalidate the cached linked operator usernames."""
        self._linked_operator_usernames = None

    # --- Mention resolution ---

    def _get_user_mention_patterns(self) -> list[tuple[re.Pattern[str], str]]:
        if self._user_mention_patterns is None:
            self._user_mention_patterns = build_user_mention_patterns(self.user_directory)
        return self._user_mention_patterns

    def _get_channel_mention_patterns(self) -> list[tuple[re.Pattern[str], str]]:
        if self._channel_mention_patterns is None:
            self._channel_mention_patterns = build_channel_mention_patterns(self._channel_ids)
        return self._channel_mention_patterns

    def _resolve_outbound_mentions(self, text: str) -> str:
        if not self._expand_mentions:
            return text
        if not self.user_directory and not self._channel_ids:
            return text
        return expand_mentions(
            text,
            self._get_user_mention_patterns(),
            self._get_channel_mention_patterns(),
        )

    async def _resolve_user(self, user_id: str) -> str:
        profile = self.user_directory.get(user_id)
        if profile:
            return profile.display_name
        app = self.require_app()
        try:
            resp = await api_call("users.info", lambda: app.client.users_info(user=user_id))
            self._upsert_user(resp.get("user", {}))
            profile = self.user_directory.get(user_id)
            if profile:
                return profile.display_name
        except Exception:
            logger.warning("Failed to resolve Slack user %s, using raw ID", user_id)
        return user_id

    async def _resolve_channel(self, channel_id: str) -> str:
        cached = self._channels.get(channel_id)
        if cached:
            return cached
        if channel_id.startswith("D"):
            return "DM"
        app = self.require_app()
        try:
            resp = await api_call(
                "conversations.info",
                lambda: app.client.conversations_info(channel=channel_id),
            )
            channel = resp.get("channel", {})
            raw_name = channel.get("name") or ""
            name = f"#{raw_name}" if raw_name else channel_id
            self._channels[channel_id] = name
            if raw_name:
                self._channel_ids[raw_name] = channel_id
            return name
        except Exception:
            logger.warning("Failed to resolve Slack channel %s, using raw ID", channel_id)
            return channel_id

    @override
    async def resolve_channel_id(self, channel: str) -> str | None:
        if channel[0:1] in "CGD" and channel.isalnum() and channel == channel.upper():
            return channel
        name = channel.lstrip("#")
        return self._channel_ids.get(name)

    async def _render_slack_text(self, text: str) -> str:
        return await render_slack_text(text, self._resolve_user)

    # --- Messaging ---

    @override
    async def send(self, channel_id: str, text: str, thread_id: str | None = None) -> str:
        app = self.require_app()
        text = self._resolve_outbound_mentions(text)
        kwargs = {"channel": channel_id, "text": _mrkdwn.convert(text)}
        if thread_id:
            kwargs["thread_ts"] = thread_id
        resp = await api_call(
            "chat.postMessage",
            lambda: app.client.chat_postMessage(**kwargs),
        )
        return resp["ts"]

    @override
    async def update(self, channel_id: str, message_id: str, text: str) -> None:
        app = self.require_app()
        text = self._resolve_outbound_mentions(text)
        await api_call(
            "chat.update",
            lambda: app.client.chat_update(
                channel=channel_id, ts=message_id, text=_mrkdwn.convert(text)
            ),
        )

    @override
    async def delete(self, channel_id: str, message_id: str) -> None:
        app = self.require_app()
        await api_call(
            "chat.delete",
            lambda: app.client.chat_delete(channel=channel_id, ts=message_id),
        )

    # --- File handling ---

    _MAX_DOWNLOAD = 50 * 1024 * 1024

    @override
    async def download_file(self, attachment: Attachment) -> bytes:
        session = self._get_http_session()
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        async with session.get(attachment.url, headers=headers) as resp:
            resp.raise_for_status()
            length = resp.content_length
            if length is not None and length > self._MAX_DOWNLOAD:
                raise ValueError(f"File too large: {length} bytes (limit {self._MAX_DOWNLOAD})")
            if length is not None:
                return await resp.read()
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
        app = self.require_app()
        kwargs: dict = {
            "channel": channel_id,
            "content": file_data,
            "filename": filename,
        }
        if thread_id:
            kwargs["thread_ts"] = thread_id
        resp = await api_call(
            "files.upload_v2",
            lambda: app.client.files_upload_v2(**kwargs),
        )
        file_info = resp.get("file", {})
        shares = file_info.get("shares", {})
        for share_type in ("public", "private"):
            for channel_shares in shares.get(share_type, {}).values():
                if channel_shares:
                    return channel_shares[0].get("ts", "")
        logger.warning("send_file: no message ts found in upload response for %s", filename)
        return ""

    # --- Context resolution ---

    @override
    async def resolve_context(self, msg: IncomingMessage) -> MessageContext:
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

    @override
    async def get_message_context(self, msg: IncomingMessage) -> list[str]:
        lines = [
            f"- message_id: `{msg.message_id}`",
            f"- channel_id: `{msg.channel_id}`",
        ]
        if msg.root_message_id != msg.message_id:
            lines.append(f"- thread_id: `{msg.root_message_id}`")
        return [
            '<context_snapshot source="message_meta">\n'
            "Current message metadata (use these IDs with Slack tools):\n\n"
            + "\n".join(lines)
            + "\n</context_snapshot>"
        ]

    @override
    async def get_thread_context(self, msg: IncomingMessage) -> str | None:
        app = self.require_app()
        try:
            resp = await api_call(
                "conversations.replies",
                lambda: app.client.conversations_replies(
                    channel=msg.channel_id, ts=msg.root_message_id
                ),
            )
        except Exception:
            logger.warning("Failed to fetch thread replies for %s", msg.root_message_id)
            return None

        replies = resp.get("messages", [])
        replies = [r for r in replies if r.get("ts") != msg.message_id]
        if not replies:
            return None

        total = len(replies)
        if total > self._MAX_THREAD_MESSAGES:
            replies = replies[-self._MAX_THREAD_MESSAGES :]

        prefix = (
            f"(showing last {self._MAX_THREAD_MESSAGES} of {total} messages)\n"
            if total > self._MAX_THREAD_MESSAGES
            else ""
        )
        return prefix + await self.format_messages(replies)

    # --- Tools ---

    @override
    def get_tools(self) -> list[ToolDef]:
        return get_slack_tools(self)

    # --- Prompt injection ---

    def format_channel_list(self, query: str = "") -> list[str]:
        return format_channel_list(self._channels, self._channel_info, query)

    @override
    def get_prompt_extra(self) -> str:
        return build_slack_prompt_extra(
            user_directory=self.user_directory,
            channels=self._channels,
            channel_info=self._channel_info,
            inject_users=self._inject_users_into_prompt,
            inject_channels=self._inject_channels_into_prompt,
        )

    # --- Message formatting ---

    async def format_messages(self, messages: list[dict]) -> str:
        return await format_slack_messages(
            messages,
            resolve_user=self._resolve_user,
            render_text=self._render_slack_text,
        )

    # --- Reaction handling ---

    async def _handle_reaction(self, event: dict, *, added: bool) -> None:
        emoji = event.get("reaction", "")
        user_id = event.get("user", "")
        item = event.get("item", {})
        channel_id = item.get("channel", "")
        message_ts = item.get("ts", "")

        if not all((emoji, user_id, channel_id, message_ts)):
            return
        if user_id == self._bot_user_id:
            return

        user_name = await self._resolve_user(user_id)
        channel_name = await self._resolve_channel(channel_id)
        action = "added" if added else "removed"
        text = (
            f"Reaction :{emoji}: {action} by {user_name} on message {message_ts} in {channel_name}"
        )
        logger.info(
            "Reaction event: :%s: %s by %s (%s) in %s",
            emoji,
            action,
            user_name,
            user_id,
            channel_name,
        )
        await self._emit_system_event(channel_id, message_ts, text)

    # --- Dispatch ---

    async def _dispatch(
        self,
        event: dict,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
    ) -> None:
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return
        text = await self._render_slack_text(event.get("text", ""))
        attachments = extract_attachments(event)

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
            transport_name=self.agent_name,
            is_private=(event.get("channel_type") == "im"),
            attachments=attachments,
            created_at=slack_ts_to_float(message_id),
        )
        await on_message(msg)
