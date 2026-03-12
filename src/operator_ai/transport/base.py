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
    was_mentioned: bool = False
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

    def to_prompt(self, workspace: str = "", operator_home: str = "") -> str:
        if self.username:
            user_line = f"- User: {self.username} ({self.user_name} via {self.platform})"
        else:
            user_line = f"- User: {self.user_name} (`{self.user_id}`)"
        lines = [
            "# Context",
            "",
            f"- Platform: {self.platform}",
        ]
        if self.chat_type:
            lines.append(f"- Chat type: {self.chat_type}")
        lines += [
            f"- Channel: {self.channel_name} (`{self.channel_id}`)",
            user_line,
        ]
        if self.roles:
            lines.append(f"- Roles: {', '.join(self.roles)}")
        if self.timezone:
            lines.append(f"- Timezone: {self.timezone}")
        elif self.username:
            lines.append("- Timezone: *not set — please ask the user for their timezone*")
        if workspace:
            lines.append(f"- Workspace: `{workspace}`")
        if operator_home:
            lines.append(f"- Operator home: `{operator_home}` (also `$OPERATOR_HOME`)")
        return "\n".join(lines)


class Transport(ABC):
    name: str
    agent_name: str
    platform: str

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
        return f"{self.platform}:{self.name}:{msg.channel_id}:{msg.root_message_id}"

    async def resolve_channel_id(self, channel: str) -> str | None:
        """Resolve a channel name or ID to a platform channel ID."""
        return channel

    def get_tools(self) -> list[ToolDef]:
        """Return transport-specific tools to merge into the agent's tool set."""
        return []

    async def get_thread_context(self, msg: IncomingMessage) -> str | None:
        """Return formatted thread history for context injection. None if not applicable."""
        return None

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

    async def update(
        self, channel_id: str, message_id: str, text: str, thread_id: str | None = None
    ) -> None:
        """Update an existing message. No-op by default."""

    async def delete(self, channel_id: str, message_id: str, thread_id: str | None = None) -> None:
        """Delete a message. No-op by default."""

    def get_prompt_extra(self) -> str:
        """Return extra prompt content (e.g. available channels) to append to system prompt."""
        return ""
