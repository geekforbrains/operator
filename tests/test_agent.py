from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

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
