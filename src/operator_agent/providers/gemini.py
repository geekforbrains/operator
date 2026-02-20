"""Gemini CLI provider."""

from __future__ import annotations

import os

from . import BaseProvider, StreamEvent


def _format_status(event: dict) -> str | None:
    """Extract human-readable status from a Gemini JSON event."""
    if event.get("type") != "tool_use":
        return None

    tool_name = event.get("tool_name", "")
    params = event.get("parameters", {})

    match tool_name:
        case "read_file":
            path = params.get("file_path", "file")
            return f"Reading {os.path.basename(path)}..."
        case "read_many_files":
            return "Reading files..."
        case "write_file":
            path = params.get("file_path", "file")
            return f"Writing {os.path.basename(path)}..."
        case "replace":
            path = params.get("file_path", "file")
            return f"Editing {os.path.basename(path)}..."
        case "run_shell_command":
            cmd = params.get("command", "")
            short = cmd[:50] + "..." if len(cmd) > 50 else cmd
            return f"Running {short}"
        case "list_directory":
            return f"Listing {params.get('dir_path', '.')}..."
        case "glob" | "find_files":
            pattern = params.get("pattern", "")
            return f"Finding {pattern}..."
        case "web_fetch":
            url = params.get("url", "")
            return f"Fetching {url[:40]}..."
        case "google_web_search":
            query = params.get("query", "")
            return f"Searching: {query}..."
        case _:
            return f"Using {tool_name}..."


class GeminiProvider(BaseProvider):
    name = "gemini"

    def stdout_limit(self):
        return 10 * 1024 * 1024

    def build_command(self, prompt, model, session_id=None):
        cmd = [
            self.path,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--yolo",
            "-m",
            model,
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        return cmd

    def parse_event(self, event):
        events = []
        event_type = event.get("type", "")

        status = _format_status(event)
        if status:
            events.append(StreamEvent(kind="status", text=status))

        if event_type == "message" and event.get("role") == "assistant":
            content = event.get("content", "")
            if content:
                events.append(StreamEvent(kind="response", text=content))

        elif event_type == "error":
            msg = event.get("message", "")
            if msg:
                events.append(StreamEvent(kind="error", text=msg))
                events.append(StreamEvent(kind="status", text=f"Error: {msg[:40]}"))

        elif event_type == "init":
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                events.append(StreamEvent(kind="session", session_id=session_id))

        return events

    def format_response(self, parts: list[str]) -> str:
        """Gemini streams content in chunks; join them all."""
        return "".join(parts)
