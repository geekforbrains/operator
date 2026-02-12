"""Claude CLI provider."""

from __future__ import annotations

import os
from pathlib import Path

from . import BaseProvider, StreamEvent


def _format_tool_status(tool_name: str, tool_input: dict) -> str:
    """Format tool usage into human-readable status."""
    match tool_name:
        case "Read":
            path = tool_input.get("file_path", "file")
            return f"Reading {os.path.basename(path)}..."
        case "Write":
            path = tool_input.get("file_path", "file")
            return f"Writing {os.path.basename(path)}..."
        case "Edit":
            path = tool_input.get("file_path", "file")
            return f"Editing {os.path.basename(path)}..."
        case "Bash":
            cmd = tool_input.get("command", "")
            short = cmd[:50] + "..." if len(cmd) > 50 else cmd
            return f"Running {short}"
        case "Glob":
            pattern = tool_input.get("pattern", "")
            return f"Finding {pattern}..."
        case "Grep":
            pattern = tool_input.get("pattern", "")
            return f"Searching for {pattern}..."
        case "WebFetch":
            url = tool_input.get("url", "")
            return f"Fetching {url[:40]}..."
        case "WebSearch":
            query = tool_input.get("query", "")
            return f"Searching: {query}..."
        case "Task":
            return "Running subagent..."
        case _:
            return f"Using {tool_name}..."


def _get_project_dir(working_dir: str) -> str:
    """Derive Claude's project session directory from working_dir."""
    resolved = str(Path(working_dir).resolve())
    mangled = resolved.replace("/", "-")
    return os.path.expanduser(f"~/.claude/projects/{mangled}")


class ClaudeProvider(BaseProvider):
    name = "claude"

    def build_command(self, prompt, model, session_id=None):
        return [
            self.path,
            "-p",
            "--continue",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--model",
            model,
            prompt,
        ]

    def parse_event(self, event):
        events = []
        event_type = event.get("type", "")

        if event_type == "assistant":
            message = event.get("message", {})
            content = message.get("content", [])
            for block in content:
                if block.get("type") == "tool_use":
                    events.append(
                        StreamEvent(
                            kind="status",
                            text=_format_tool_status(
                                block.get("name", ""), block.get("input", {})
                            ),
                        )
                    )
                elif block.get("type") == "text":
                    block_text = block.get("text", "")
                    if block_text:
                        events.append(StreamEvent(kind="response", text=block_text))

        elif event_type == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, str) and result_text:
                events.append(StreamEvent(kind="response", text=result_text))

        return events

    def stdout_limit(self):
        return 10 * 1024 * 1024

    def clear_session(self, session_id, working_dir):
        project_dir = Path(_get_project_dir(working_dir))
        removed = 0
        if project_dir.is_dir():
            for f in project_dir.glob("*.jsonl"):
                f.unlink()
                removed += 1
        return f"{removed} session file{'s' if removed != 1 else ''} deleted"
