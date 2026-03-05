from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import aiohttp
from markdown_to_mrkdwn import SlackMarkdownConverter
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from typing_extensions import override

from operator_ai.tools.registry import ToolDef
from operator_ai.transport.base import Attachment, IncomingMessage, MessageContext, Transport

logger = logging.getLogger("operator.transport.slack")

MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")
_mrkdwn = SlackMarkdownConverter()

CACHE_REFRESH_SECONDS = 15 * 60  # 15 minutes
MAX_API_ATTEMPTS = 3
BASE_RETRY_SECONDS = 1.0


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


class SlackTransport(Transport):
    def __init__(
        self,
        name: str,
        agent_name: str,
        bot_token: str,
        app_token: str,
    ):
        self.name = name
        self.platform = "slack"
        self.agent_name = agent_name
        self._bot_token = bot_token
        self._app_token = app_token
        self._app: AsyncApp | None = None
        self._handler: AsyncSocketModeHandler | None = None
        self._background_tasks: set[asyncio.Task] = set()

        # In-memory caches (populated by _refresh_cache)
        self._users: dict[str, str] = {}  # user_id -> display name
        self._channels: dict[str, str] = {}  # channel_id -> #name
        self._channel_ids: dict[str, str] = {}  # name (no #) -> channel_id
        self._channel_info: dict[str, str] = {}  # channel_id -> topic/purpose snippet
        self._refresh_task: asyncio.Task | None = None

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
            self._create_task(self._dispatch(event, on_message))

        @self._app.event("message")
        async def handle_message(event: dict, say):  # noqa: ARG001
            # Only handle DMs (im channels) — app_mention covers channels
            if event.get("channel_type") == "im" and not event.get("bot_id"):
                self._create_task(self._dispatch(event, on_message))

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        self._refresh_task = asyncio.create_task(self._refresh_cache_loop())
        logger.info("Starting Slack transport '%s'", self.name)
        await self._handler.start_async()

    @override
    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
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

    # --- Cache refresh ---

    async def _refresh_cache_loop(self) -> None:
        """Run immediately on start, then every CACHE_REFRESH_SECONDS."""
        try:
            while True:
                try:
                    await self._fetch_all_channels()
                    logger.debug("Slack channel cache refreshed (%d channels)", len(self._channels))
                except Exception:
                    logger.warning("Failed to refresh Slack channel cache", exc_info=True)
                await asyncio.sleep(CACHE_REFRESH_SECONDS)
        except asyncio.CancelledError:
            return

    async def _fetch_all_channels(self) -> None:
        """Paginate conversations_list and populate caches atomically."""
        app = self._require_app()
        channels: dict[str, str] = {}
        channel_ids: dict[str, str] = {}
        channel_info: dict[str, str] = {}

        cursor = None
        while True:
            params: dict = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            request_params = dict(params)
            resp = await self._api_call(
                "conversations.list",
                lambda rp=request_params: app.client.conversations_list(**rp),
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

        # Atomic swap
        self._channels = channels
        self._channel_ids = channel_ids
        self._channel_info = channel_info

    # --- Messaging ---

    @override
    async def send(self, channel_id: str, text: str, thread_id: str | None = None) -> str:
        app = self._require_app()
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
                data = await resp.read()
                if len(data) > self._MAX_DOWNLOAD:
                    raise ValueError(
                        f"File too large: {len(data)} bytes (limit {self._MAX_DOWNLOAD})"
                    )
                return data

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
        return MessageContext(
            platform="slack",
            channel_id=msg.channel_id,
            channel_name=await self._resolve_channel(msg.channel_id),
            user_id=msg.user_id,
            user_name=await self._resolve_user(raw_user_id),
        )

    async def _resolve_user(self, user_id: str) -> str:
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
        # Already a Slack channel ID
        if channel.startswith(("C", "G", "D")) and len(channel) > 1:
            return channel
        name = channel.lstrip("#")
        cached = self._channel_ids.get(name)
        if cached:
            return cached
        # Cache miss — refresh and retry
        try:
            await self._fetch_all_channels()
        except Exception:
            logger.warning("Failed to refresh channel cache while resolving '%s'", channel)
        return self._channel_ids.get(name)

    # --- Transport-scoped tools ---

    def _format_channel_list(self) -> list[str]:
        """Format the cached channel list as markdown bullet lines."""
        lines: list[str] = []
        for ch_id, ch_name in sorted(self._channels.items(), key=lambda x: x[1]):
            info = self._channel_info.get(ch_id, "")
            suffix = f" — {info}" if info else ""
            lines.append(f"- {ch_name} (`{ch_id}`){suffix}")
        return lines

    @override
    def get_tools(self) -> list[ToolDef]:
        async def list_channels() -> str:
            """List available Slack channels the bot can post to."""
            if not self._channels:
                return "No channels cached yet. Try again shortly."
            return "\n".join(self._format_channel_list())

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
                list_channels,
                "List available Slack channels the bot can post to, with their IDs and descriptions.",
            ),
            ToolDef(
                read_channel,
                "Read recent messages from a Slack channel. Use this to see what's been discussed.",
            ),
            ToolDef(
                read_thread,
                "Read messages from a specific Slack thread. Use this to get full context on a conversation.",
            ),
        ]

    @override
    def get_prompt_extra(self) -> str:
        lines = [
            "# Messaging",
            "",
            "Use `send_message` with a channel name (e.g. `#general`) or channel ID.",
            "It returns a Slack message timestamp you can pass as `thread_id` to reply in a thread.",
        ]
        if self._channels:
            lines += ["", "# Available Channels", ""]
            lines += self._format_channel_list()
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
                dt = datetime.fromtimestamp(ts, tz=UTC).astimezone()
                time_str = dt.strftime("%-I:%M %p")
            except (TypeError, ValueError):
                time_str = "unknown time"
            text = m.get("text", "")
            text = MENTION_RE.sub("", text).strip()
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
    ) -> None:
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            # Ignore edited/deleted/system message variants,
            # but allow file_share (message with uploaded files).
            return
        text = event.get("text", "")
        text = MENTION_RE.sub("", text).strip()

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
            attachments=attachments,
        )
        await on_message(msg)
