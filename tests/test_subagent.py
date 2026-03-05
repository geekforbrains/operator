"""Tests for spawn_agent context resolution."""

from __future__ import annotations

import pytest

from operator_ai.tools.subagent import _resolve_agent_context


class FakeAgentConfig:
    def __init__(self, sandbox: bool = True) -> None:
        self.sandbox = sandbox
        self.models = ["anthropic/claude-sonnet-4-6"]
        self.max_iterations = None
        self.context_ratio = None
        self.max_output_tokens = None
        self.permissions = None


class FakeConfig:
    def __init__(self) -> None:
        self.defaults = type(
            "D",
            (),
            {"models": ["openai/gpt-4.1"], "max_iterations": 25, "context_ratio": 0.5},
        )()
        self.agents = {
            "researcher": FakeAgentConfig(sandbox=False),
        }

    def agent_models(self, name: str) -> list[str]:
        a = self.agents.get(name)
        return a.models if a and a.models else self.defaults.models

    def agent_max_iterations(self, name: str) -> int:
        a = self.agents.get(name)
        return a.max_iterations if a and a.max_iterations else self.defaults.max_iterations

    def agent_workspace(self, name: str) -> str:
        return f"/home/.operator/agents/{name}/workspace"

    def agent_context_ratio(self, name: str) -> float:
        a = self.agents.get(name)
        return a.context_ratio if a and a.context_ratio else self.defaults.context_ratio

    def agent_max_output_tokens(self, name: str) -> int | None:
        a = self.agents.get(name)
        return a.max_output_tokens if a and a.max_output_tokens else None

    def agent_sandboxed(self, name: str) -> bool:
        a = self.agents.get(name)
        return a.sandbox if a is not None else True

    def agent_tool_filter(self, name: str):  # noqa: ARG002
        return None


def test_resolve_none_returns_current() -> None:
    current = {"models": ["m1"], "workspace": "/ws"}
    result = _resolve_agent_context(None, current)
    assert result is current


def test_resolve_empty_string_returns_current() -> None:
    current = {"models": ["m1"], "workspace": "/ws"}
    result = _resolve_agent_context("", current)
    assert result is current


def test_resolve_known_agent() -> None:
    config = FakeConfig()
    current = {"models": ["m1"], "workspace": "/ws", "config": config}
    result = _resolve_agent_context("researcher", current)
    assert result["models"] == ["anthropic/claude-sonnet-4-6"]
    assert "researcher" in result["workspace"]
    assert result["sandboxed"] is False


def test_resolve_unknown_agent_raises() -> None:
    config = FakeConfig()
    current = {"models": ["m1"], "config": config}
    with pytest.raises(ValueError, match="unknown agent 'ghost'"):
        _resolve_agent_context("ghost", current)


def test_resolve_without_config_returns_current() -> None:
    current = {"models": ["m1"], "workspace": "/ws"}
    result = _resolve_agent_context("anything", current)
    assert result is current
