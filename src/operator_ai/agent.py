from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import litellm

from operator_ai.config import Config, ensure_shared_symlink
from operator_ai.prompts import CACHE_BOUNDARY
from operator_ai.tools import registry as tool_registry
from operator_ai.tools import set_workspace, subagent
from operator_ai.tools.context import ROLE_GATED_TOOLS, get_user_context
from operator_ai.tools.registry import ToolDef
from operator_ai.truncation import prepare_messages_for_model

logger = logging.getLogger("operator.agent")


def _apply_cache_control(messages: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    """Add Anthropic cache breakpoints to system prompt and conversation history.

    Places up to 3 breakpoints (Anthropic allows 4 max):
      1. Stable system prompt prefix (SYSTEM.md + AGENT.md + skills)
      2. Penultimate user/assistant message — caches prior conversation history
      3. (reserved for future use)

    The stable prefix is cached across conversations.  The conversation
    breakpoint rolls forward each turn so prior history is served from cache.

    Returns messages unchanged for non-Anthropic models.
    """
    if not model.startswith("anthropic/"):
        return messages

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
                    {"type": "text", "text": dynamic},
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
    # Find the last two user-role indices (excluding system).
    user_indices = [i for i, m in enumerate(result) if m.get("role") == "user"]
    if len(user_indices) >= 2:
        # Mark the penultimate user message — everything before it is cached.
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
            # Already block format — add cache_control to the last block
            new_blocks = list(content)
            last = {**new_blocks[-1], "cache_control": {"type": "ephemeral"}}
            new_blocks[-1] = last
            result[target] = {**msg, "content": new_blocks}

    return result


async def run_agent(
    messages: list[dict[str, Any]],
    models: list[str],
    max_iterations: int,
    workspace: str,
    on_message: Callable[[str], Awaitable[None]] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    on_tool_call: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    depth: int = 0,
    context_ratio: float = 0.0,
    max_output_tokens: int | None = None,
    extra_tools: list[ToolDef] | None = None,
    usage: dict[str, int] | None = None,
    tool_filter: Callable[[str], bool] | None = None,
    shared_dir: Path | None = None,
    sandboxed: bool = True,
    config: Config | None = None,
) -> str:
    """Core agentic loop: LLM -> tool exec -> repeat until text response.

    on_message is called with each text response from the LLM — both
    intermediate "thinking" messages (before tool calls) and the final answer.
    check_cancelled is called between iterations — should raise to abort.
    models is a fallback chain — on LLM error, the next model is tried.
    """
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    if shared_dir is not None:
        ensure_shared_symlink(ws, shared_dir)
    set_workspace(ws, sandboxed=sandboxed)

    # Configure subagent tool with current context
    subagent.configure(
        {
            "models": models,
            "max_iterations": max_iterations,
            "workspace": workspace,
            "depth": depth,
            "context_ratio": context_ratio,
            "max_output_tokens": max_output_tokens,
            "extra_tools": extra_tools,
            "usage": usage,
            "tool_filter": tool_filter,
            "shared_dir": shared_dir,
            "sandboxed": sandboxed,
            "config": config,
        }
    )

    tools = tool_registry.get_tools()
    if extra_tools:
        tools = tools + list(extra_tools)
    if tool_filter is not None:
        all_names = [t.name for t in tools]
        tools = [t for t in tools if tool_filter(t.name)]
        filtered_out = set(all_names) - {t.name for t in tools}
        if filtered_out:
            logger.info("permissions: filtered out tools: %s", ", ".join(sorted(filtered_out)))
        logger.debug(
            "permissions: %d tools available: %s", len(tools), ", ".join(t.name for t in tools)
        )
    tools_by_name = {t.name: t for t in tools}
    tool_defs = [t.to_openai_tool() for t in tools]

    if not models:
        raise ValueError("no models configured")

    for iteration in range(max_iterations):
        if check_cancelled:
            check_cancelled()

        step = f"[iter {iteration + 1}/{max_iterations}]"

        # Signal "thinking" before LLM call
        if on_tool_call:
            await on_tool_call("", {})

        # Try each model in the fallback chain
        response = None
        last_error: Exception | None = None
        for model in models:
            model_messages = prepare_messages_for_model(messages, model, context_ratio)
            model_messages = _apply_cache_control(model_messages, model)
            logger.debug("%s calling %s", step, model)

            kwargs: dict[str, Any] = {
                "model": model,
                "messages": model_messages,
            }
            if tool_defs:
                kwargs["tools"] = tool_defs

            # Resolve max output tokens: config override > model default
            if max_output_tokens is not None:
                kwargs["max_tokens"] = max_output_tokens
            else:
                try:
                    info = litellm.get_model_info(model)
                    model_max = info.get("max_output_tokens")
                    if model_max:
                        kwargs["max_tokens"] = model_max
                except Exception:
                    logger.warning(
                        "%s get_model_info failed for %s, max_tokens not set", step, model
                    )

            try:
                response = await litellm.acompletion(**kwargs)
                if last_error is not None:
                    logger.info("%s recovered using fallback model %s", step, model)
                last_error = None
                break
            except Exception as e:
                last_error = e
                if model != models[-1]:
                    logger.warning(
                        "%s model %s failed (%s: %s), trying next",
                        step,
                        model,
                        type(e).__name__,
                        e,
                    )

        if last_error is not None:
            raise last_error

        if not getattr(response, "choices", None):
            raise RuntimeError("model returned no choices")

        if usage is not None and hasattr(response, "usage") and response.usage:
            u = response.usage
            usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + (u.prompt_tokens or 0)
            usage["completion_tokens"] = usage.get("completion_tokens", 0) + (
                u.completion_tokens or 0
            )
            # Anthropic: cache_read_input_tokens / cache_creation_input_tokens
            # OpenAI: prompt_tokens_details.cached_tokens
            cached_read = getattr(u, "cache_read_input_tokens", 0) or 0
            if not cached_read:
                ptd = getattr(u, "prompt_tokens_details", None)
                if ptd:
                    cached_read = getattr(ptd, "cached_tokens", 0) or 0
            usage["cache_read_input_tokens"] = usage.get("cache_read_input_tokens", 0) + cached_read
            usage["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0) + (
                getattr(u, "cache_creation_input_tokens", 0) or 0
            )

        choice = response.choices[0]
        assistant_msg = choice.message.model_dump(exclude_none=True)
        messages.append(assistant_msg)
        full_content = _extract_text_content(choice.message.content)
        tool_calls = (
            [tc.model_dump() for tc in choice.message.tool_calls]
            if choice.message.tool_calls
            else None
        )

        # Send every text response as a new message
        if full_content and on_message:
            await on_message(full_content)

        # If no tool calls, we're done
        if not tool_calls:
            logger.info("%s done — final response (%d chars)", step, len(full_content or ""))
            return full_content or ""

        # Execute tool calls
        for tc in tool_calls:
            if check_cancelled:
                check_cancelled()
            func_name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments") or ""
            try:
                parsed_args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                parsed_args = None
                logger.warning(
                    "%s malformed JSON in tool args for %s: %s",
                    step,
                    func_name,
                    raw_args[:200],
                )
            if parsed_args is not None and not isinstance(parsed_args, dict):
                parsed_args = None
                logger.warning("%s non-object tool args for %s", step, func_name)
            args = parsed_args or {}

            # Signal tool execution
            if on_tool_call:
                await on_tool_call(func_name, args)

            if parsed_args is None:
                result = f"[error: invalid tool args for '{func_name}']"
                logger.warning("%s invalid args for tool %s, call skipped", step, func_name)
            elif (tool_def := tools_by_name.get(func_name)) is None:
                result = f"[error: unknown tool '{func_name}']"
                logger.warning("%s unknown tool: %s", step, func_name)
            else:
                result = ""

                # Role gate: block execution if the user lacks the required role
                required_role = ROLE_GATED_TOOLS.get(func_name)
                if required_role:
                    user_ctx = get_user_context()
                    if not user_ctx or required_role not in user_ctx.roles:
                        result = f"[error: this tool requires the '{required_role}' role]"
                        logger.warning(
                            "%s role gate: %s requires '%s'", step, func_name, required_role
                        )

                if not result:
                    logger.info("%s tool %s(%s)", step, func_name, _truncate(str(args), 150))
                    try:
                        raw_result = await tool_def.func(**args)
                    except Exception as e:
                        result = f"[error: {e}]"
                        logger.exception("%s tool %s failed: %s", step, func_name, e)
                    else:
                        result = _normalize_tool_result(raw_result)
                        logger.info("%s tool %s → %d chars", step, func_name, len(result))

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )

    logger.warning("max iterations (%d) reached", max_iterations)
    return "[max iterations reached]"


def _truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "..." if len(s) > max_len else s


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _normalize_tool_result(result: Any) -> str:
    if result is None:
        return "[no output]"
    if isinstance(result, str):
        return result or "[no output]"
    try:
        return json.dumps(result, ensure_ascii=True, default=str)
    except Exception:
        return str(result)
