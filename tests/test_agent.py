from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import operator_ai.agent as agent_module
from operator_ai.agent import run_agent
from operator_ai.config import Config
from operator_ai.request_context import CURRENT_TIME_HEADER, inject_current_time


class _FakeAssistantMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None

    def model_dump(self, exclude_none: bool = True) -> dict[str, str]:  # noqa: ARG002
        return {"role": "assistant", "content": self.content}


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeAssistantMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = None


def _config(timezone: str = "America/Vancouver") -> Config:
    return Config(
        runtime={"timezone": timezone},
        defaults={"models": ["openai/gpt-4.1"]},
        agents={"operator": {}},
    )


def test_run_agent_injects_current_time_only_into_live_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _FakeResponse("done")

    monkeypatch.setattr("operator_ai.agent.tool_registry.get_tools", lambda: [])
    monkeypatch.setattr("operator_ai.agent.litellm.acompletion", fake_acompletion)
    monkeypatch.setattr(
        "operator_ai.agent.inject_current_time",
        lambda messages, config: inject_current_time(
            messages,
            config,
            now=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
        ),
    )

    messages = [
        {"role": "system", "content": "# System"},
        {"role": "user", "content": "What time is it?"},
    ]

    result = asyncio.run(
        run_agent(
            messages=messages,
            models=["openai/gpt-4.1"],
            max_iterations=1,
            workspace=str(tmp_path),
            context_ratio=0.0,
            max_output_tokens=64,
            config=_config(),
        )
    )

    assert result == "done"
    assert messages[0]["content"] == "# System"

    request_messages = captured["messages"]
    assert isinstance(request_messages, list)
    assert CURRENT_TIME_HEADER in request_messages[0]["content"]
    assert "America/Vancouver" in request_messages[0]["content"]


def test_run_agent_keeps_anthropic_cache_boundary_stable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _FakeResponse("done")

    monkeypatch.setattr("operator_ai.agent.tool_registry.get_tools", lambda: [])
    monkeypatch.setattr("operator_ai.agent.litellm.acompletion", fake_acompletion)
    monkeypatch.setattr(
        "operator_ai.agent.inject_current_time",
        lambda messages, config: inject_current_time(
            messages,
            config,
            now=datetime(2026, 3, 6, 17, 45, tzinfo=UTC),
        ),
    )

    asyncio.run(
        run_agent(
            messages=[
                {"role": "system", "content": "# Stable System"},
                {"role": "user", "content": "Ping"},
            ],
            models=["anthropic/claude-sonnet-4-6"],
            max_iterations=1,
            workspace=str(tmp_path),
            context_ratio=0.0,
            max_output_tokens=64,
            config=_config(),
        )
    )

    request_messages = captured["messages"]
    assert isinstance(request_messages, list)

    system_content = request_messages[0]["content"]
    assert isinstance(system_content, list)
    assert system_content[0]["text"] == "# Stable System"
    assert system_content[0]["cache_control"] == {"type": "ephemeral"}
    assert CURRENT_TIME_HEADER in system_content[1]["text"]
    assert "# Stable System" not in system_content[1]["text"]


def test_run_agent_maps_thinking_to_reasoning_effort_when_supported(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _FakeResponse("done")

    agent_module._supports_reasoning_effort.cache_clear()
    monkeypatch.setattr("operator_ai.agent.tool_registry.get_tools", lambda: [])
    monkeypatch.setattr("operator_ai.agent.litellm.acompletion", fake_acompletion)
    monkeypatch.setattr(
        "operator_ai.agent.litellm.get_supported_openai_params",
        lambda *_args, **_kwargs: ["reasoning_effort"],
    )

    with caplog.at_level(logging.INFO, logger="operator.agent"):
        result = asyncio.run(
            run_agent(
                messages=[
                    {"role": "system", "content": "# System"},
                    {"role": "user", "content": "Plan this"},
                ],
                models=["openai/o3"],
                max_iterations=1,
                workspace=str(tmp_path),
                context_ratio=0.0,
                max_output_tokens=64,
                thinking="high",
            )
        )

    assert result == "done"
    assert captured["reasoning_effort"] == "high"
    assert "thinking=high -> reasoning_effort=high" in caplog.text


def test_run_agent_fallback_omits_reasoning_effort_and_sanitizes_history(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "anthropic/claude-sonnet-4-6":
            raise RuntimeError("primary failed")
        return _FakeResponse("done")

    def fake_supported_params(model: str) -> list[str]:
        if model == "anthropic/claude-sonnet-4-6":
            return ["reasoning_effort"]
        return ["max_tokens"]

    agent_module._supports_reasoning_effort.cache_clear()
    monkeypatch.setattr("operator_ai.agent.tool_registry.get_tools", lambda: [])
    monkeypatch.setattr("operator_ai.agent.litellm.acompletion", fake_acompletion)
    monkeypatch.setattr(
        "operator_ai.agent.litellm.get_supported_openai_params",
        fake_supported_params,
    )

    messages = [
        {"role": "system", "content": "# System"},
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
        },
        {"role": "user", "content": "Try again"},
    ]

    with caplog.at_level(logging.DEBUG, logger="operator.agent"):
        result = asyncio.run(
            run_agent(
                messages=messages,
                models=["anthropic/claude-sonnet-4-6", "openai/gpt-4.1"],
                max_iterations=1,
                workspace=str(tmp_path),
                context_ratio=0.0,
                max_output_tokens=64,
                thinking="high",
            )
        )

    assert result == "done"
    assert len(calls) == 2
    assert calls[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in calls[1]

    first_assistant = calls[0]["messages"][1]
    second_assistant = calls[1]["messages"][1]
    assert first_assistant["content"] == [{"type": "text", "text": "Visible answer"}]
    assert second_assistant["content"] == [{"type": "text", "text": "Visible answer"}]
    assert "reasoning_content" not in first_assistant
    assert "thinking_blocks" not in first_assistant
    assert "provider_specific_fields" not in first_assistant
    assert "reasoning_content" not in second_assistant
    assert "thinking_blocks" not in second_assistant
    assert "provider_specific_fields" not in second_assistant
    assert "history for anthropic/claude-sonnet-4-6 dropped" in caplog.text
    assert "requested thinking=high but reasoning control unsupported" in caplog.text
