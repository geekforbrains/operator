"""Minimal CLI transport for `operator job run` — prints send_message output to stdout."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable

from operator_ai.transport.base import IncomingMessage, MessageContext, Transport


class CliTransport(Transport):
    """Transport that prints messages to stdout instead of sending to a platform."""

    platform = "cli"

    def __init__(self, agent_name: str) -> None:
        self.name = agent_name
        self.agent_name = agent_name
        self._counter = 0

    async def start(self, on_message: Callable[[IncomingMessage], Awaitable[None]]) -> None:
        raise NotImplementedError("CliTransport does not accept inbound messages")

    async def stop(self) -> None:
        pass

    async def send(self, channel_id: str, text: str, thread_id: str | None = None) -> str:
        self._counter += 1
        header = f"[send_message → {channel_id}]"
        if thread_id:
            header += f" (thread: {thread_id})"
        sys.stderr.write(f"\n{header}\n{text}\n")
        return f"cli-{self._counter}"

    async def resolve_context(self, msg: IncomingMessage) -> MessageContext:  # noqa: ARG002
        return MessageContext(
            platform="cli",
            channel_id="cli",
            channel_name="cli",
            user_id="cli",
            user_name="cli",
        )

    async def resolve_channel_id(self, channel: str) -> str | None:
        return channel
