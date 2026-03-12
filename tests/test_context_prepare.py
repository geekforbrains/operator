"""Tests for operator_ai.context.prepare_context pipeline."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from operator_ai.context import (
    HARD_CLEAR_PLACEHOLDER,
    SOFT_TRIM_HEAD,
    SOFT_TRIM_TAIL,
    _apply_cache_breakpoints,
    _clean_and_render,
    _compress_tool_results,
    _enforce_budget,
    _soft_trim_text,
    prepare_context,
)
from operator_ai.message_timestamps import MESSAGE_CREATED_AT_KEY, attach_message_created_at
from operator_ai.prompts import CACHE_BOUNDARY

VANCOUVER = ZoneInfo("America/Vancouver")


# ---------------------------------------------------------------------------
# Step 1: Clean + render
# ---------------------------------------------------------------------------


class TestCleanAndRender:
    def test_timestamps_rendered_on_user_messages(self) -> None:
        messages = [
            {"role": "system", "content": "System prompt"},
            attach_message_created_at(
                {"role": "user", "content": "Hello"},
                created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
            ),
        ]
        result = _clean_and_render(messages, model="openai/gpt-4.1", tz=VANCOUVER)
        assert result[1]["content"].startswith("[Friday, 2026-03-06T09:45:00-08:00]\n")
        assert result[1]["content"].endswith("Hello")
        assert MESSAGE_CREATED_AT_KEY not in result[1]

    def test_timestamp_key_stripped_and_rendered_in_utc_without_tz(self) -> None:
        messages = [
            attach_message_created_at(
                {"role": "user", "content": "Hi"},
                created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
            ),
        ]
        result = _clean_and_render(messages, model="openai/gpt-4.1", tz=None)
        assert MESSAGE_CREATED_AT_KEY not in result[0]
        # Renders in UTC when no timezone provided
        assert result[0]["content"].startswith("[Friday, 2026-03-06T17:45:00+00:00]")
        assert result[0]["content"].endswith("Hi")

    def test_reasoning_blocks_stripped_from_assistant(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "redacted_thinking", "data": "secret"},
                    {"type": "text", "text": "Visible answer"},
                ],
                "reasoning_content": "step by step",
                "thinking_blocks": [{"type": "thinking", "thinking": "internal"}],
                "provider_specific_fields": {"source": "anthropic"},
            }
        ]
        result = _clean_and_render(messages, model="openai/gpt-4.1", tz=None)
        assert result[0]["content"] == [{"type": "text", "text": "Visible answer"}]
        assert "reasoning_content" not in result[0]
        assert "thinking_blocks" not in result[0]
        assert "provider_specific_fields" not in result[0]

    def test_assistant_with_all_reasoning_blocks_gets_empty_string(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                ],
            }
        ]
        result = _clean_and_render(messages, model="openai/gpt-4.1", tz=None)
        assert result[0]["content"] == ""

    def test_system_and_tool_messages_passed_through(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        ]
        result = _clean_and_render(messages, model="openai/gpt-4.1", tz=None)
        assert result[0]["content"] == "You are helpful"
        assert result[1]["content"] == "result"
        assert result[1]["tool_call_id"] == "tc1"

    def test_original_messages_not_mutated(self) -> None:
        messages = [
            attach_message_created_at(
                {"role": "user", "content": "Hello"},
                created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
            ),
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "Answer"},
                ],
                "reasoning_content": "blah",
            },
        ]
        originals = copy.deepcopy(messages)
        _clean_and_render(messages, model="openai/gpt-4.1", tz=VANCOUVER)
        assert messages == originals


# ---------------------------------------------------------------------------
# Step 2: Tool result compression
# ---------------------------------------------------------------------------


class TestToolResultCompression:
    def test_last_n_tool_results_untouched(self) -> None:
        messages = self._make_tool_messages(8)
        _compress_tool_results(messages, keep=5, soft_trim=10)
        # Last 5 should be untouched
        for msg in messages[-5:]:
            assert msg["content"].startswith("result_")

    def test_soft_trim_preserves_head_and_tail(self) -> None:
        long_content = "A" * 2000
        messages = [
            {"role": "tool", "tool_call_id": "old", "content": long_content},
            *self._make_tool_messages(5),  # these are the "keep" tier
        ]
        _compress_tool_results(messages, keep=5, soft_trim=10)
        result = messages[0]["content"]
        assert result.startswith("A" * SOFT_TRIM_HEAD)
        assert result.endswith("A" * SOFT_TRIM_TAIL)
        assert "trimmed 2000 chars" in result

    def test_soft_trim_skips_short_content(self) -> None:
        short = "x" * 100  # shorter than SOFT_TRIM_HEAD + SOFT_TRIM_TAIL
        messages = [
            {"role": "tool", "tool_call_id": "old", "content": short},
            *self._make_tool_messages(5),
        ]
        _compress_tool_results(messages, keep=5, soft_trim=10)
        assert messages[0]["content"] == short  # unchanged

    def test_hard_clear_replaces_with_placeholder(self) -> None:
        messages = [
            {"role": "tool", "tool_call_id": "ancient", "content": "old data"},
            *[
                {"role": "tool", "tool_call_id": f"mid_{i}", "content": f"mid_{i}"}
                for i in range(10)
            ],
            *self._make_tool_messages(5),
        ]
        _compress_tool_results(messages, keep=5, soft_trim=10)
        assert messages[0]["content"] == HARD_CLEAR_PLACEHOLDER

    def test_fewer_tool_results_than_keep(self) -> None:
        messages = self._make_tool_messages(3)
        originals = copy.deepcopy(messages)
        _compress_tool_results(messages, keep=5, soft_trim=10)
        assert messages == originals

    def test_zero_tool_results(self) -> None:
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "hi"},
        ]
        originals = copy.deepcopy(messages)
        _compress_tool_results(messages, keep=5, soft_trim=10)
        assert messages == originals

    def test_block_format_tool_content(self) -> None:
        long_text = "B" * 2000
        messages = [
            {
                "role": "tool",
                "tool_call_id": "old",
                "content": [{"type": "text", "text": long_text}],
            },
            *self._make_tool_messages(5),
        ]
        _compress_tool_results(messages, keep=5, soft_trim=10)
        block = messages[0]["content"][0]
        assert block["text"].startswith("B" * SOFT_TRIM_HEAD)
        assert "trimmed 2000 chars" in block["text"]

    def test_tiers_respect_reverse_position(self) -> None:
        """Tool results are tiered by reverse position, not list order."""
        messages = [
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": "a1",
                "tool_calls": [
                    {"id": "tc1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "X" * 2000},  # oldest tool
            {"role": "user", "content": "q2"},
            {
                "role": "assistant",
                "content": "a2",
                "tool_calls": [
                    {"id": "tc2", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "tc2", "content": "Y" * 2000},  # newest tool
        ]
        _compress_tool_results(messages, keep=1, soft_trim=1)
        # tc2 (position 0 from end) -> keep
        assert messages[5]["content"] == "Y" * 2000
        # tc1 (position 1 from end) -> soft_trim
        assert "trimmed 2000 chars" in messages[2]["content"]

    @staticmethod
    def _make_tool_messages(n: int) -> list[dict[str, Any]]:
        return [
            {"role": "tool", "tool_call_id": f"tc_{i}", "content": f"result_{i}"} for i in range(n)
        ]


# ---------------------------------------------------------------------------
# Step 3: Budget enforcement
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    def test_under_budget_returns_unchanged(self) -> None:
        messages = [
            {"role": "system", "content": "short"},
            {"role": "user", "content": "hi"},
        ]
        with patch("operator_ai.context._get_max_input_tokens", return_value=100_000):
            result = _enforce_budget(messages, model="openai/gpt-4.1", context_ratio=0.5)
        assert result is messages  # same object, fast path

    def test_over_budget_drops_oldest_exchanges(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first question " * 100},
            {"role": "assistant", "content": "first answer " * 100},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ]

        def fake_token_count(_model: str, msgs: list, *, tools=None) -> int:  # noqa: ARG001
            return sum(len(str(m.get("content", ""))) for m in msgs) // 4

        with (
            patch("operator_ai.context._get_max_input_tokens", return_value=200),
            patch("operator_ai.context._token_count", side_effect=fake_token_count),
        ):
            result = _enforce_budget(messages, model="openai/gpt-4.1", context_ratio=0.5)

        # System and latest exchange should be kept
        assert result[0]["role"] == "system"
        assert any("second question" in str(m.get("content", "")) for m in result)
        # Oldest exchange should be dropped
        assert not any("first question" in str(m.get("content", "")) for m in result)

    def test_single_exchange_not_dropped(self) -> None:
        messages = [
            {"role": "system", "content": "s" * 10000},
            {"role": "user", "content": "only question"},
        ]

        def fake_token_count(_model: str, _msgs: list, *, tools=None) -> int:  # noqa: ARG001
            return 99999  # always over budget

        with (
            patch("operator_ai.context._get_max_input_tokens", return_value=100),
            patch("operator_ai.context._token_count", side_effect=fake_token_count),
        ):
            result = _enforce_budget(messages, model="openai/gpt-4.1", context_ratio=0.5)

        assert len(result) == 2  # can't drop anything

    def test_no_model_info_returns_unchanged(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        with patch("operator_ai.context._get_max_input_tokens", return_value=None):
            result = _enforce_budget(messages, model="unknown/model", context_ratio=0.5)
        assert result is messages

    def test_budget_counts_tool_schemas(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hi"},
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Searches for something",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "additionalProperties": False,
                    },
                },
            }
        ]
        captured: list[list[dict[str, Any]] | None] = []

        def fake_token_count(_model: str, _msgs: list, *, tools=None) -> int:
            captured.append(tools)
            return 10

        with (
            patch("operator_ai.context._get_max_input_tokens", return_value=100),
            patch("operator_ai.context._token_count", side_effect=fake_token_count),
        ):
            result = _enforce_budget(messages, model="openai/gpt-4.1", context_ratio=0.5, tools=tools)

        assert result is messages
        assert captured == [tools]

    def test_image_only_exchange_still_uses_precise_budget_count(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            },
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second reply"},
        ]

        def fake_token_count(_model: str, msgs: list, *, tools=None) -> int:  # noqa: ARG001
            if any(m.get("role") == "user" and isinstance(m.get("content"), list) for m in msgs):
                return 999
            return 10

        with (
            patch("operator_ai.context._get_max_input_tokens", return_value=100),
            patch("operator_ai.context._token_count", side_effect=fake_token_count),
        ):
            result = _enforce_budget(messages, model="openai/gpt-4.1", context_ratio=0.5)

        assert result[0]["role"] == "system"
        assert not any(isinstance(m.get("content"), list) for m in result)
        assert any(m.get("content") == "second question" for m in result)


# ---------------------------------------------------------------------------
# Step 4: Cache breakpoints
# ---------------------------------------------------------------------------


class TestCacheBreakpoints:
    def test_anthropic_system_with_cache_boundary(self) -> None:
        messages = [
            {"role": "system", "content": f"stable{CACHE_BOUNDARY}dynamic"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response"},
            {"role": "user", "content": "second"},
        ]
        result = _apply_cache_breakpoints(messages)

        # System prompt should be split into 2 blocks, both cached
        sys_content = result[0]["content"]
        assert isinstance(sys_content, list)
        assert len(sys_content) == 2
        assert sys_content[0]["text"] == "stable"
        assert sys_content[0]["cache_control"] == {"type": "ephemeral"}
        assert sys_content[1]["text"] == "dynamic"
        assert sys_content[1]["cache_control"] == {"type": "ephemeral"}

        # Penultimate user message should have cache_control
        first_user = result[1]
        assert isinstance(first_user["content"], list)
        assert first_user["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_anthropic_system_without_cache_boundary(self) -> None:
        messages = [
            {"role": "system", "content": "just a prompt"},
            {"role": "user", "content": "hello"},
        ]
        result = _apply_cache_breakpoints(messages)

        sys_content = result[0]["content"]
        assert isinstance(sys_content, list)
        assert len(sys_content) == 1
        assert sys_content[0]["text"] == "just a prompt"
        assert sys_content[0]["cache_control"] == {"type": "ephemeral"}

    def test_non_anthropic_skipped_at_top_level(self) -> None:
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "hi"},
        ]
        result = prepare_context(messages, "openai/gpt-4.1")
        # Should not have cache breakpoints
        assert isinstance(result[0]["content"], str)

    def test_single_user_message_no_penultimate_breakpoint(self) -> None:
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "only one"},
        ]
        result = _apply_cache_breakpoints(messages)
        # No penultimate user message to cache
        assert isinstance(result[1]["content"], str)

    def test_penultimate_user_with_block_content(self) -> None:
        messages = [
            {"role": "system", "content": "prompt"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            },
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        result = _apply_cache_breakpoints(messages)
        # cache_control added to the last block of penultimate user
        user_content = result[1]["content"]
        assert isinstance(user_content, list)
        assert user_content[-1].get("cache_control") == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Integration: full pipeline
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_no_mutation_of_input(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            attach_message_created_at(
                {"role": "user", "content": "hello"},
                created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
            ),
            {
                "role": "assistant",
                "content": "reply",
                "reasoning_content": "internal",
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "tool output"},
        ]
        originals = copy.deepcopy(messages)
        prepare_context(messages, "openai/gpt-4.1", tz=VANCOUVER)
        assert messages == originals

    def test_tool_call_id_pairing_valid_after_transforms(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "file contents " * 500},
            {"role": "user", "content": "now summarize"},
        ]
        result = prepare_context(
            messages, "openai/gpt-4.1", tool_results_keep=0, tool_results_soft_trim=1
        )
        # tool_call_id should still be present
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "tc_1"

    def test_anthropic_full_pipeline(self) -> None:
        messages = [
            {"role": "system", "content": f"stable{CACHE_BOUNDARY}dynamic"},
            attach_message_created_at(
                {"role": "user", "content": "first"},
                created_at=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
            ),
            {"role": "assistant", "content": "reply"},
            attach_message_created_at(
                {"role": "user", "content": "second"},
                created_at=datetime(2026, 3, 6, 18, 0, tzinfo=UTC),
            ),
        ]
        result = prepare_context(
            messages,
            "anthropic/claude-sonnet-4-6",
            tz=VANCOUVER,
        )

        # System prompt should have cache breakpoints
        sys_content = result[0]["content"]
        assert isinstance(sys_content, list)
        assert sys_content[0]["cache_control"] == {"type": "ephemeral"}

        # First user message should have timestamp and cache breakpoint
        first_user = result[1]
        assert isinstance(first_user["content"], list)
        assert "2026-03-06T09:45:00-08:00" in first_user["content"][0]["text"]

        # Second user message should have timestamp
        second_user = result[3]
        assert "2026-03-06T10:00:00-08:00" in str(second_user["content"])

    def test_context_ratio_zero_skips_budget(self) -> None:
        messages = [
            {"role": "system", "content": "x" * 100_000},
            {"role": "user", "content": "hi"},
        ]
        # Should not call litellm at all
        result = prepare_context(messages, "openai/gpt-4.1", context_ratio=0.0)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Soft trim helper
# ---------------------------------------------------------------------------


class TestSoftTrimText:
    def test_short_text_unchanged(self) -> None:
        text = "short"
        assert _soft_trim_text(text) is text

    def test_exactly_at_boundary(self) -> None:
        text = "x" * (SOFT_TRIM_HEAD + SOFT_TRIM_TAIL)
        assert _soft_trim_text(text) is text

    def test_long_text_trimmed(self) -> None:
        text = "A" * 500 + "B" * 1000 + "C" * 200
        result = _soft_trim_text(text)
        assert result.startswith("A" * 500)
        assert result.endswith("C" * 200)
        assert "trimmed 1700 chars" in result
