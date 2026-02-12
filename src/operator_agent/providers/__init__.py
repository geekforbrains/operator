"""Provider abstraction for CLI agents."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Unified event type emitted by all providers."""

    kind: Literal["status", "response", "session", "error"]
    text: str = ""
    session_id: str | None = None


class BaseProvider(ABC):
    """Base class for CLI agent providers."""

    name: str

    def __init__(self, path: str):
        self.path = path

    @abstractmethod
    def build_command(
        self, prompt: str, model: str, session_id: str | None = None
    ) -> list[str]:
        """Build the CLI command to execute."""

    @abstractmethod
    def parse_event(self, event: dict) -> list[StreamEvent]:
        """Parse a raw JSON event into StreamEvents."""

    def parse_line(self, line: str) -> dict | None:
        """Parse a raw stdout line into a JSON dict. Returns None to skip."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None

    def stderr_to_stdout(self) -> bool:
        """If True, merge stderr into stdout."""
        return False

    def stdout_limit(self) -> int | None:
        """Buffer limit for stdout, or None for default."""
        return None

    def format_response(self, parts: list[str]) -> str:
        """Combine response parts into final text. Default: last part wins."""
        return parts[-1] if parts else ""

    def clear_session(self, session_id: str | None, working_dir: str) -> str:
        """Clear session data. Returns human-readable summary."""
        return "session cleared" if session_id else "no session"


PROVIDER_NAMES = ["claude", "codex", "gemini"]


def get_provider(name: str, path: str) -> BaseProvider:
    """Create a provider instance by name."""
    # Lazy imports: subclasses import from this module, so top-level would be circular.
    from .claude import ClaudeProvider
    from .codex import CodexProvider
    from .gemini import GeminiProvider

    classes: dict[str, type[BaseProvider]] = {
        "claude": ClaudeProvider,
        "codex": CodexProvider,
        "gemini": GeminiProvider,
    }
    cls = classes.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name}")
    return cls(path)
