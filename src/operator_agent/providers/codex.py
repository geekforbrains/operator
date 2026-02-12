"""Codex CLI provider."""

from __future__ import annotations

import json

from . import BaseProvider, StreamEvent


def _format_status(event: dict) -> str | None:
    """Extract human-readable status from a Codex JSON event."""
    event_type = event.get("type", "")
    item = event.get("item", {})
    item_type = item.get("type", "")

    if event_type == "item.started":
        if item_type == "command_execution":
            cmd = item.get("command", "")
            if "-lc " in cmd:
                cmd = cmd.split("-lc ", 1)[1].strip("'\"")
            short = cmd[:50] + "..." if len(cmd) > 50 else cmd
            return f"Running {short}"
        if item_type == "reasoning":
            return "Thinking..."
        if item_type == "file_changes":
            return "Editing files..."
        if item_type == "web_searches":
            return "Searching the web..."
        if item_type == "mcp_tool_calls":
            return "Using tool..."

    if event_type == "item.completed" and item_type == "reasoning":
        text = item.get("text", "")
        if text:
            clean = text.strip("*").strip()
            short = clean[:40] + "..." if len(clean) > 40 else clean
            return short

    return None


class CodexProvider(BaseProvider):
    name = "codex"

    def build_command(self, prompt, model, session_id=None):
        if session_id:
            return [
                self.path,
                "exec",
                "resume",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
                "-m",
                model,
                session_id,
                prompt,
            ]
        return [
            self.path,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-m",
            model,
            prompt,
        ]

    def parse_line(self, line):
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None

    def parse_event(self, event):
        events = []
        event_type = event.get("type", "")

        status = _format_status(event)
        if status:
            events.append(StreamEvent(kind="status", text=status))

        if event_type == "turn.started":
            events.append(StreamEvent(kind="status", text="Working..."))

        elif event_type == "error":
            msg = event.get("message")
            if isinstance(msg, str) and msg:
                events.append(StreamEvent(kind="error", text=msg))
                if "reconnect" in msg.lower():
                    events.append(StreamEvent(kind="status", text="Reconnecting..."))

        elif event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value:
                    events.append(StreamEvent(kind="response", text=text_value))

        elif event_type == "turn.failed":
            msg = event.get("message", "")
            if msg:
                events.append(StreamEvent(kind="error", text=msg))

        elif event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                events.append(StreamEvent(kind="session", session_id=thread_id))

        return events

    def stderr_to_stdout(self):
        return True
