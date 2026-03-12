"""Single-pass context preparation for model input."""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

import litellm

from operator_ai.message_timestamps import (
    MESSAGE_CREATED_AT_KEY,
    _prefix_content,
    build_message_timestamp_prefix,
)
from operator_ai.prompts import CACHE_BOUNDARY

logger = logging.getLogger("operator.context")

# --- Constants ---

SOFT_TRIM_HEAD = 500  # chars to keep from start of soft-trimmed tool results
SOFT_TRIM_TAIL = 200  # chars to keep from end of soft-trimmed tool results
HARD_CLEAR_PLACEHOLDER = "[tool result cleared]"

_REASONING_MESSAGE_KEYS = frozenset(
    {"reasoning_content", "thinking_blocks", "provider_specific_fields"}
)
_REASONING_CONTENT_TYPES = frozenset({"thinking", "redacted_thinking"})


def prepare_context(
    messages: list[dict[str, Any]],
    model: str,
    *,
    context_ratio: float = 0.0,
    tz: ZoneInfo | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_results_keep: int = 5,
    tool_results_soft_trim: int = 10,
) -> list[dict[str, Any]]:
    """Prepare messages for model input in a single pipeline.

    Steps:
      1. Clean + render (timestamps, reasoning strip) — always runs
      2. Tool result compression — always runs
      3. Budget enforcement (exchange dropping) — only when over context_ratio
      4. Anthropic cache breakpoints — only for anthropic/ models

    Never mutates the original *messages* list or its message dicts.
    """
    # Step 1: Clean + render
    result = _clean_and_render(messages, model=model, tz=tz)

    # Step 2: Tool result compression
    _compress_tool_results(result, keep=tool_results_keep, soft_trim=tool_results_soft_trim)

    # Step 3: Budget enforcement
    if context_ratio > 0:
        result = _enforce_budget(result, model=model, context_ratio=context_ratio, tools=tools)

    # Step 4: Anthropic cache breakpoints
    if model.startswith("anthropic/"):
        result = _apply_cache_breakpoints(result)

    return result


# ---------------------------------------------------------------------------
# Step 1: Clean + render
# ---------------------------------------------------------------------------


def _clean_and_render(
    messages: list[dict[str, Any]],
    *,
    model: str,
    tz: ZoneInfo | None,
) -> list[dict[str, Any]]:
    """Walk messages once, building a new output list.

    - system: shallow copy as-is
    - user: shallow copy, pop _operator_created_at, render timestamp prefix
    - assistant: shallow copy, strip reasoning metadata
    - tool: shallow copy as-is
    """
    output: list[dict[str, Any]] = []
    stripped_messages = 0
    stripped_items = 0

    for message in messages:
        role = message.get("role")

        if role == "system":
            output.append(dict(message))

        elif role == "user":
            clean = dict(message)
            created_at = clean.pop(MESSAGE_CREATED_AT_KEY, None)
            if isinstance(created_at, (int, float)) and created_at:
                prefix = build_message_timestamp_prefix(tz, created_at=created_at)
                if prefix:
                    clean["content"] = _prefix_content(clean.get("content"), prefix)
            output.append(clean)

        elif role == "assistant":
            clean = dict(message)
            removed = 0

            # Strip reasoning metadata keys
            for key in _REASONING_MESSAGE_KEYS:
                if key in clean:
                    clean.pop(key)
                    removed += 1

            # Filter out thinking/redacted_thinking content blocks
            content = clean.get("content")
            if isinstance(content, list):
                filtered = [b for b in content if not _is_reasoning_block(b)]
                removed_blocks = len(content) - len(filtered)
                if removed_blocks:
                    clean["content"] = filtered if filtered else ""
                    removed += removed_blocks

            if removed:
                stripped_messages += 1
                stripped_items += removed
            output.append(clean)

        else:
            # tool and any other role
            output.append(dict(message))

    if stripped_messages:
        logger.debug(
            "history for %s dropped %d reasoning metadata item(s) from %d assistant message(s)",
            model,
            stripped_items,
            stripped_messages,
        )

    return output


def _is_reasoning_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in _REASONING_CONTENT_TYPES


# ---------------------------------------------------------------------------
# Step 2: Tool result compression
# ---------------------------------------------------------------------------


def _compress_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep: int,
    soft_trim: int,
) -> None:
    """Compress tool results in-place by tier (keep / soft-trim / hard-clear).

    Tool results are identified by role == "tool". Tier assignment is by
    reverse position: count tool messages from the end.
    """
    # Collect indices of tool messages in reverse order
    tool_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "tool":
            tool_indices.append(i)

    if not tool_indices:
        return

    # tool_indices[0] is the last tool message, tool_indices[-1] is the oldest
    for position, idx in enumerate(tool_indices):
        if position < keep:
            # Keep tier: untouched
            continue
        elif position < keep + soft_trim:
            # Soft trim tier
            _soft_trim_tool_result(messages[idx])
        else:
            # Hard clear tier
            _hard_clear_tool_result(messages[idx])


def _soft_trim_tool_result(message: dict[str, Any]) -> None:
    """Trim tool result content to head + tail with a marker in between."""
    content = message.get("content")
    if isinstance(content, str):
        trimmed = _soft_trim_text(content)
        if trimmed is not content:
            message["content"] = trimmed
    elif isinstance(content, list):
        message["content"] = _soft_trim_blocks(content)


def _soft_trim_text(text: str) -> str:
    """Return soft-trimmed version of text, or the original if short enough."""
    min_length = SOFT_TRIM_HEAD + SOFT_TRIM_TAIL
    if len(text) <= min_length:
        return text
    head = text[:SOFT_TRIM_HEAD]
    tail = text[-SOFT_TRIM_TAIL:]
    return f"{head}\n...[trimmed {len(text)} chars]...\n{tail}"


def _soft_trim_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a new block list with text blocks soft-trimmed. Never mutates originals."""
    result: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                trimmed = _soft_trim_text(text)
                if trimmed is not text:
                    result.append({**block, "text": trimmed})
                    continue
        result.append(block)
    return result


def _hard_clear_tool_result(message: dict[str, Any]) -> None:
    """Replace tool result content with a placeholder."""
    message["content"] = HARD_CLEAR_PLACEHOLDER


# ---------------------------------------------------------------------------
# Step 3: Budget enforcement
# ---------------------------------------------------------------------------


def _enforce_budget(
    messages: list[dict[str, Any]],
    *,
    model: str,
    context_ratio: float,
    tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Drop oldest exchange groups if over the context budget.

    Uses the full request shape (messages + tools) so the budget reflects the
    actual model call, not just the text portion of conversation history.
    """
    max_input_tokens = _get_max_input_tokens(model)
    if not max_input_tokens:
        return messages

    budget_tokens = max(1, int(max_input_tokens * context_ratio))

    current_tokens = _token_count(model, messages, tools=tools)
    if current_tokens is None or current_tokens <= budget_tokens:
        return messages

    # Identify exchange groups
    system_len = _system_block_length(messages)
    groups = _group_exchanges(messages, start_idx=system_len)
    if len(groups) <= 1:
        # Only one exchange group (the current one) — can't drop anything
        return messages

    # The last group is never dropped (it's the current exchange)
    droppable = len(groups) - 1

    # Binary search: find minimum number of groups to drop
    lo, hi = 1, droppable
    best_drop = droppable  # worst case: drop everything except last

    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _messages_without_groups(messages, groups, system_len, drop_count=mid)
        count = _token_count(model, candidate, tools=tools)
        if count is None:
            # Can't measure — fall back to dropping all removable
            break
        if count <= budget_tokens:
            best_drop = mid
            hi = mid - 1
        else:
            lo = mid + 1

    result = _messages_without_groups(messages, groups, system_len, drop_count=best_drop)
    final_tokens = _token_count(model, result, tools=tools)
    logger.info(
        "Budget enforcement model=%s ratio=%.2f tokens=%d->%s msgs=%d->%d (dropped %d exchange groups)",
        model,
        context_ratio,
        current_tokens,
        final_tokens if final_tokens is not None else "?",
        len(messages),
        len(result),
        best_drop,
    )
    return result


def _system_block_length(messages: list[dict[str, Any]]) -> int:
    """Count leading system messages."""
    n = 0
    for msg in messages:
        if msg.get("role") != "system":
            break
        n += 1
    return n


def _group_exchanges(messages: list[dict[str, Any]], start_idx: int) -> list[list[int]]:
    """Group messages into exchange groups starting at each user message."""
    groups: list[list[int]] = []
    current: list[int] = []
    for idx in range(start_idx, len(messages)):
        if messages[idx].get("role") == "user" and current:
            groups.append(current)
            current = []
        current.append(idx)
    if current:
        groups.append(current)
    return groups


def _messages_without_groups(
    messages: list[dict[str, Any]],
    groups: list[list[int]],
    system_len: int,
    drop_count: int,
) -> list[dict[str, Any]]:
    """Return messages with the first *drop_count* exchange groups removed."""
    drop_indices: set[int] = set()
    for g in groups[:drop_count]:
        drop_indices.update(g)
    return [m for i, m in enumerate(messages) if i < system_len or i not in drop_indices]


def _get_max_input_tokens(model: str) -> int | None:
    try:
        info = litellm.get_model_info(model)
        return info.get("max_input_tokens")
    except Exception:
        logger.warning("get_model_info failed for model=%s, budget enforcement disabled", model)
        return None


def _token_count(
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
) -> int | None:
    try:
        return int(litellm.token_counter(model=model, messages=messages, tools=tools))
    except Exception:
        logger.warning(
            "token_counter failed for model=%s (%d messages), count unavailable",
            model,
            len(messages),
        )
        return None


# ---------------------------------------------------------------------------
# Step 4: Anthropic cache breakpoints
# ---------------------------------------------------------------------------


def _apply_cache_breakpoints(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add up to 3 Anthropic cache breakpoints.

    1. Stable system prompt prefix (before CACHE_BOUNDARY)
    2. Dynamic system prompt suffix (after CACHE_BOUNDARY)
    3. Penultimate user message (caches prior conversation history)
    """
    result: list[dict[str, Any]] = []

    # --- System prompt: split stable prefix / dynamic suffix ---
    for msg in messages:
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            content = msg["content"]
            if CACHE_BOUNDARY in content:
                stable, dynamic = content.split(CACHE_BOUNDARY, 1)
                blocks: list[dict[str, Any]] = [
                    {
                        "type": "text",
                        "text": stable,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": dynamic,
                        "cache_control": {"type": "ephemeral"},
                    },
                ]
            else:
                blocks = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            result.append({**msg, "content": blocks})
        else:
            result.append(msg)

    # --- Conversation history: cache up to the penultimate user message ---
    user_indices = [i for i, m in enumerate(result) if m.get("role") == "user"]
    if len(user_indices) >= 2:
        target = user_indices[-2]
        msg = result[target]
        content = msg.get("content")
        if isinstance(content, str):
            result[target] = {
                **msg,
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        elif isinstance(content, list):
            new_blocks = list(content)
            last = {**new_blocks[-1], "cache_control": {"type": "ephemeral"}}
            new_blocks[-1] = last
            result[target] = {**msg, "content": new_blocks}

    return result
