#!/usr/bin/env python3
"""Local harness to simulate Telegram message flow against Claude or Codex CLI.

Examples:
  ./simulate_message.py --provider codex --message "hello"
  ./simulate_message.py --provider codex --chat-id 123 --message "follow up"
  ./simulate_message.py --provider claude --message "summarise this repo"
  ./simulate_message.py --provider codex --clear --message "fresh start"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SESSIONS_FILE = Path.home() / ".operator_sim_sessions.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Keep defaults aligned with existing bot
CLAUDE_PATH = os.getenv("CLAUDE_PATH", "claude")
CLAUDE_MODEL = "opus"
WORKING_DIR = str(Path(__file__).resolve().parent)


def load_sessions() -> dict[str, dict[str, str]]:
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return {}


def save_sessions(data: dict[str, dict[str, str]]) -> None:
    SESSIONS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))


def codex_session_file_count() -> int:
    if not CODEX_SESSIONS_DIR.exists():
        return 0
    return sum(1 for p in CODEX_SESSIONS_DIR.rglob("*.jsonl") if p.is_file())


def newest_codex_session_file() -> str:
    if not CODEX_SESSIONS_DIR.exists():
        return "(none)"
    files = [p for p in CODEX_SESSIONS_DIR.rglob("*.jsonl") if p.is_file()]
    if not files:
        return "(none)"
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return str(newest)


def print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def run_codex(chat_id: str, message: str, clear: bool) -> int:
    sessions = load_sessions()
    sessions.setdefault(chat_id, {})

    if clear:
        sessions[chat_id].pop("codex", None)

    session_id = sessions[chat_id].get("codex")

    before_count = codex_session_file_count()
    before_newest = newest_codex_session_file()

    print_header("CODEX RUN")
    print(f"chat_id: {chat_id}")
    print(f"saved session_id: {session_id or '(none)'}")
    print(f"codex session files before: {before_count}")
    print(f"newest before: {before_newest}")

    if session_id:
        cmd = [
            "codex",
            "exec",
            "resume",
            "--json",
            session_id,
            message,
        ]
    else:
        cmd = [
            "codex",
            "exec",
            "--json",
            message,
        ]

    print("command:", " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        cwd=WORKING_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assistant_text_parts: list[str] = []
    last_error = ""

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        print(line)

        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        # Capture newly created/resumed thread ID for persistence.
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                sessions[chat_id]["codex"] = thread_id

        # Best-effort text capture from known/likely event shapes.
        if event_type in ("agent_message_delta", "response.output_text.delta"):
            delta = event.get("delta") or event.get("text")
            if isinstance(delta, str):
                assistant_text_parts.append(delta)

        if event_type == "response_item":
            payload = event.get("payload", {})
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                for chunk in payload.get("content", []):
                    text = chunk.get("text") or chunk.get("output_text")
                    if isinstance(text, str):
                        assistant_text_parts.append(text)

        if event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    assistant_text_parts.append(text)

        if event_type == "error":
            msg = event.get("message")
            if isinstance(msg, str):
                last_error = msg

    rc = proc.wait()

    save_sessions(sessions)

    after_count = codex_session_file_count()
    after_newest = newest_codex_session_file()

    print_header("CODEX SUMMARY")
    print(f"exit_code: {rc}")
    print(f"saved session_id now: {sessions.get(chat_id, {}).get('codex', '(none)')}")
    print(f"codex session files after: {after_count}")
    print(f"newest after: {after_newest}")

    if assistant_text_parts:
        print("assistant_text_preview:")
        print("".join(assistant_text_parts)[:2000])
    elif last_error:
        print(f"last_error: {last_error}")

    print(f"session mapping file: {SESSIONS_FILE}")
    return rc


def run_claude(chat_id: str, message: str, clear: bool) -> int:
    sessions = load_sessions()
    sessions.setdefault(chat_id, {})

    if clear:
        sessions[chat_id].pop("claude", None)

    print_header("CLAUDE RUN")
    print(f"chat_id: {chat_id}")
    print("note: claude --continue handles session continuity internally")

    cmd = [
        CLAUDE_PATH,
        "-p",
        "--continue",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model",
        CLAUDE_MODEL,
        message,
    ]

    print("command:", " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        cwd=WORKING_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    final_result = ""
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        print(line)

        stripped = line.strip()
        if not stripped.startswith("{"):
            continue

        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "result":
            result_text = event.get("result")
            if isinstance(result_text, str) and result_text:
                final_result = result_text

    rc = proc.wait()

    if final_result:
        sessions[chat_id]["claude"] = "continue-mode"
    save_sessions(sessions)

    print_header("CLAUDE SUMMARY")
    print(f"exit_code: {rc}")
    if final_result:
        print("assistant_text_preview:")
        print(final_result[:2000])
    print(f"session mapping file: {SESSIONS_FILE}")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate Telegram message routing locally")
    parser.add_argument("--provider", choices=["codex", "claude"], default="codex")
    parser.add_argument("--chat-id", default="local-test")
    parser.add_argument("--message", required=True)
    parser.add_argument(
        "--clear", action="store_true", help="Clear saved session for this provider/chat"
    )
    args = parser.parse_args()

    os.chdir(WORKING_DIR)

    if args.provider == "codex":
        return run_codex(args.chat_id, args.message, args.clear)
    return run_claude(args.chat_id, args.message, args.clear)


if __name__ == "__main__":
    sys.exit(main())
