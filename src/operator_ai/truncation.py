from __future__ import annotations

import copy
import logging
from typing import Any

import litellm

logger = logging.getLogger("operator.truncation")

TRUNCATION_MARKER = "\n...[truncated for context budget]...\n"
SHORTEN_STEPS = (4000, 2000, 1000, 500, 250, 120)


def prepare_messages_for_model(
    messages: list[dict[str, Any]],
    model: str,
    context_ratio: float,
) -> list[dict[str, Any]]:
    """Return a budget-safe view of messages for model input.

    Keeps the system prompt and latest user exchange intact.
    Drops oldest exchanges first, then shortens remaining non-user
    content if still over budget. Never mutates the original list.
    """
    if not messages or context_ratio <= 0:
        return messages

    max_input_tokens = _get_max_input_tokens(model)
    if not max_input_tokens:
        return messages

    budget_tokens = max(1, int(max_input_tokens * context_ratio))
    original_tokens = _token_count(model, messages)
    if original_tokens is None or original_tokens <= budget_tokens:
        return messages

    working = copy.deepcopy(messages)
    _drop_oldest_exchanges(working, model, budget_tokens)

    after_drop = _token_count(model, working)
    if after_drop is not None and after_drop <= budget_tokens:
        _log_trim(model, context_ratio, original_tokens, after_drop, len(messages), len(working))
        return working

    _shorten_oldest_non_user_content(working, model, budget_tokens)
    final_tokens = _token_count(model, working)
    _log_trim(model, context_ratio, original_tokens, final_tokens, len(messages), len(working))
    return working


def _log_trim(
    model: str,
    context_ratio: float,
    before_tokens: int | None,
    after_tokens: int | None,
    before_messages: int,
    after_messages: int,
) -> None:
    logger.info(
        "Context trim model=%s ratio=%.2f msgs=%d->%d tokens=%s->%s",
        model,
        context_ratio,
        before_messages,
        after_messages,
        before_tokens if before_tokens is not None else "?",
        after_tokens if after_tokens is not None else "?",
    )


def _get_max_input_tokens(model: str) -> int | None:
    try:
        info = litellm.get_model_info(model)
        return info.get("max_input_tokens")
    except Exception:
        logger.warning("get_model_info failed for model=%s, truncation disabled", model)
        return None


def _token_count(model: str, messages: list[dict[str, Any]]) -> int | None:
    try:
        return int(litellm.token_counter(model=model, messages=messages))
    except Exception:
        logger.warning(
            "token_counter failed for model=%s (%d messages), count unavailable",
            model,
            len(messages),
        )
        return None


def _system_block_length(messages: list[dict[str, Any]]) -> int:
    n = 0
    for msg in messages:
        if msg.get("role") != "system":
            break
        n += 1
    return n


def _group_exchange_indices(messages: list[dict[str, Any]], start_idx: int) -> list[list[int]]:
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


def _latest_user_group_idx(messages: list[dict[str, Any]], groups: list[list[int]]) -> int:
    for group_idx in range(len(groups) - 1, -1, -1):
        if any(messages[i].get("role") == "user" for i in groups[group_idx]):
            return group_idx
    return max(0, len(groups) - 1)


def _drop_oldest_exchanges(
    messages: list[dict[str, Any]],
    model: str,
    budget_tokens: int,
) -> bool:
    system_len = _system_block_length(messages)
    groups = _group_exchange_indices(messages, start_idx=system_len)
    if not groups:
        return False

    keep_from_group = _latest_user_group_idx(messages, groups)
    removable_groups = groups[:keep_from_group]
    if not removable_groups:
        return False

    removed: set[int] = set()

    for group in removable_groups:
        candidate_removed = removed | set(group)
        candidate = [msg for idx, msg in enumerate(messages) if idx not in candidate_removed]
        count = _token_count(model, candidate)
        if count is None:
            logger.warning("context drop aborted: token count unavailable for model=%s", model)
            return False
        removed = candidate_removed
        if count is not None and count <= budget_tokens:
            break

    if removed:
        for idx in sorted(removed, reverse=True):
            del messages[idx]
        return True
    return False


def _shorten_oldest_non_user_content(
    messages: list[dict[str, Any]],
    model: str,
    budget_tokens: int,
) -> bool:
    candidates = [
        idx
        for idx, msg in enumerate(messages)
        if msg.get("role") not in ("user", "system") and msg.get("content")
    ]

    if not candidates:
        return False

    working = copy.deepcopy(messages)
    for max_chars in SHORTEN_STEPS:
        candidate = copy.deepcopy(working)
        changed = False
        for idx in candidates:
            content = candidate[idx].get("content")
            if isinstance(content, str):
                if len(content) <= max_chars:
                    continue
                candidate[idx]["content"] = _truncate_middle(content, max_chars)
                changed = True
            elif isinstance(content, list):
                if _shorten_content_blocks(content, max_chars):
                    changed = True

        if not changed:
            continue

        count = _token_count(model, candidate)
        if count is None:
            logger.warning(
                "context shortening aborted: token count unavailable for model=%s", model
            )
            return False

        working = candidate
        if count <= budget_tokens:
            messages[:] = working
            return True

    messages[:] = working
    return False


def _shorten_content_blocks(blocks: list[dict[str, Any]], max_chars: int) -> bool:
    """Shorten text blocks within a multimodal content block list. Returns True if anything changed."""
    changed = False
    for block in blocks:
        if block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and len(text) > max_chars:
                block["text"] = _truncate_middle(text, max_chars)
                changed = True
    return changed


def _truncate_middle(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    if max_chars <= len(TRUNCATION_MARKER):
        return content[:max_chars]
    keep = max_chars - len(TRUNCATION_MARKER)
    head = keep // 2
    tail = keep - head
    return content[:head] + TRUNCATION_MARKER + content[-tail:]
