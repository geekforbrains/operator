from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from operator_ai.tools.registry import ToolDef


@dataclass
class Attachment:
    """Platform-agnostic file attachment."""

    filename: str
    content_type: str  # MIME type: image/png, application/pdf, audio/wav
    size: int  # bytes
    url: str  # platform download URL (ephemeral)
    platform_id: str = ""  # slack file ID, telegram file_id, etc.


@dataclass
class IncomingMessage:
    text: str
    user_id: str
    channel_id: str
    message_id: str
    root_message_id: str
    transport_name: str
    is_private: bool = False
    attachments: list[Attachment] = field(default_factory=list)
    created_at: float | None = None


@dataclass
class MessageContext:
    """Resolved context for system prompt injection."""

    platform: str
    channel_id: str
    channel_name: str
    user_id: str
    user_name: str
    username: str = ""
    roles: list[str] = field(default_factory=list)
    timezone: str | None = None
    chat_type: str = ""


class Transport(ABC):
    agent_name: str
    platform: str

    _system_event_handler: Callable[[str, str, str], Awaitable[None]] | None = None

    def set_system_event_handler(
        self,
        handler: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        """Register a handler for system events.

        Handler signature: (channel_id, message_id, text) -> None
        """
        self._system_event_handler = handler

    async def _emit_system_event(self, channel_id: str, message_id: str, text: str) -> None:
        """Emit a system event if a handler is registered."""
        if self._system_event_handler is not None:
            await self._system_event_handler(channel_id, message_id, text)

    @abstractmethod
    async def start(self, on_message: Callable[[IncomingMessage], Awaitable[None]]) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, channel_id: str, text: str, thread_id: str | None = None) -> str:
        """Send a message, return platform message ID."""

    @abstractmethod
    async def resolve_context(self, msg: IncomingMessage) -> MessageContext:
        """Resolve platform IDs to human-readable names."""

    def build_conversation_id(self, msg: IncomingMessage) -> str:
        """Build a canonical conversation ID from an incoming message."""
        return f"{self.platform}:{self.agent_name}:{msg.channel_id}:{msg.root_message_id}"

    async def resolve_channel_id(self, channel: str) -> str | None:
        """Resolve a channel name or ID to a platform channel ID."""
        return channel

    def get_tools(self) -> list[ToolDef]:
        """Return transport-specific tools to merge into the agent's tool set."""
        return []

    async def get_thread_context(
        self, msg: IncomingMessage, *, after_ts: str | None = None
    ) -> str | None:
        """Return formatted thread history for context injection.

        If *after_ts* is provided, only return messages newer than that timestamp.
        Returns None when there are no relevant messages.
        """
        return None

    async def get_message_context(self, msg: IncomingMessage) -> list[str]:
        """Return context blocks to prepend to the user message.

        Each string is a self-contained block (e.g. wrapped in
        ``<context_snapshot>`` tags).  The dispatcher collects blocks from
        all sources and joins them before the user text.  Empty by default.
        """
        return []

    async def download_file(self, attachment: Attachment) -> bytes:
        """Download file bytes for an attachment. Override in transports that support files."""
        raise NotImplementedError(f"{type(self).__name__} does not support file downloads")

    async def send_file(
        self,
        channel_id: str,
        file_data: bytes,
        filename: str,
        thread_id: str | None = None,
    ) -> str:
        """Upload a file to a channel, return platform message ID.

        Override in transports that support files.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support file uploads")

    async def update(self, channel_id: str, message_id: str, text: str) -> None:
        """Update an existing message. No-op by default."""

    async def delete(self, channel_id: str, message_id: str) -> None:
        """Delete a message. No-op by default."""

    async def set_status(self, channel_id: str, thread_id: str | None = None) -> None:
        """Show a processing indicator. No-op by default."""

    async def clear_status(self, channel_id: str, thread_id: str | None = None) -> None:
        """Clear the processing indicator. No-op by default."""

    def get_prompt_extra(self) -> str:
        """Return extra prompt content (e.g. available channels) to append to system prompt."""
        return ""
