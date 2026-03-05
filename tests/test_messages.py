from __future__ import annotations

from operator_ai.messages import trim_incomplete_tool_turns


def test_empty_input() -> None:
    assert trim_incomplete_tool_turns([]) == []


def test_trim_incomplete_tool_turn_removes_orphaned_assistant_and_tail() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "t1"},
        {"role": "assistant", "content": "a2", "tool_calls": [{"id": "call_2"}]},
        {"role": "user", "content": "u2"},
    ]

    trimmed = trim_incomplete_tool_turns(messages)

    assert trimmed == messages[:4]


def test_trim_incomplete_tool_turn_keeps_complete_history() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "t1"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "u2"},
    ]

    trimmed = trim_incomplete_tool_turns(messages)

    assert trimmed == messages


def test_trim_multi_tool_call_with_partial_results() -> None:
    messages = [
        {"role": "user", "content": "u1"},
        {
            "role": "assistant",
            "content": "a1",
            "tool_calls": [{"id": "call_1"}, {"id": "call_2"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "t1"},
        # call_2 result missing
    ]

    trimmed = trim_incomplete_tool_turns(messages)

    assert trimmed == [{"role": "user", "content": "u1"}]


def test_trim_multi_tool_call_all_present() -> None:
    messages = [
        {"role": "user", "content": "u1"},
        {
            "role": "assistant",
            "content": "a1",
            "tool_calls": [{"id": "call_1"}, {"id": "call_2"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "t1"},
        {"role": "tool", "tool_call_id": "call_2", "content": "t2"},
        {"role": "assistant", "content": "done"},
    ]

    trimmed = trim_incomplete_tool_turns(messages)

    assert trimmed == messages
