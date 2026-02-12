"""Transport abstraction for message delivery."""

from __future__ import annotations

from typing import Any, Protocol


class TransportContext(Protocol):
    """Protocol for sending messages back to the user."""

    async def reply(self, text: str) -> None:
        """Send a text message."""
        ...

    async def reply_status(self, text: str) -> Any:
        """Send an initial status message. Returns an opaque handle."""
        ...

    async def edit_status(self, handle: Any, text: str) -> None:
        """Edit a status message by handle."""
        ...

    async def delete_status(self, handle: Any) -> None:
        """Delete a status message by handle."""
        ...
