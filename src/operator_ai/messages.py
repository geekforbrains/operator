from __future__ import annotations

from typing import Any


def trim_incomplete_tool_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim conversation history to the first valid boundary.

    Returns a copy of messages with any incomplete assistant tool-call turn and
    all trailing messages removed. This prevents malformed history from being
    sent back to the model.
    """
    cutoff = _first_incomplete_tool_turn_index(messages)
    if cutoff is None:
        return messages
    return messages[:cutoff]


def _first_incomplete_tool_turn_index(messages: list[dict[str, Any]]) -> int | None:
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            continue
        if not isinstance(tool_calls, list):
            return idx

        expected_ids: set[str] = set()
        for tc in tool_calls:
            if not isinstance(tc, dict):
                return idx
            tc_id = tc.get("id")
            if not isinstance(tc_id, str) or not tc_id:
                return idx
            expected_ids.add(tc_id)

        found_ids = {
            follow["tool_call_id"]
            for follow in messages[idx + 1 :]
            if follow.get("role") == "tool" and isinstance(follow.get("tool_call_id"), str)
        }
        if not expected_ids.issubset(found_ids):
            return idx
    return None
