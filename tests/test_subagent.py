"""Tests for spawn_agent context resolution."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from operator_ai.config import Config
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import MESSAGE_CREATED_AT_KEY
from operator_ai.run_prompt import ChatEnvelope, JobEnvelope
from operator_ai.tools import memory as memory_tools
from operator_ai.tools import state as state_tools
from operator_ai.tools import subagent
from operator_ai.tools.context import UserContext, set_user_context
from operator_ai.tools.subagent import (
    SubagentContext,
    _resolve_agent_context,
    _user_can_access_agent,
    spawn_agent,
)
from operator_ai.transport.base import MessageContext


class FakeAgentConfig:
    def __init__(self) -> None:
        self.models = ["anthropic/claude-sonnet-4-6"]
        self.thinking = "high"
        self.max_iterations = None
        self.context_ratio = None
        self.max_output_tokens = None
        self.permissions = None


@dataclass
class FakeRoleConfig:
    agents: list[str] = field(default_factory=list)


class FakeConfig:
    def __init__(self, roles: dict[str, FakeRoleConfig] | None = None) -> None:
        self.base_dir = Path("/home/.operator")
        self.defaults = type(
            "D",
            (),
            {
                "models": ["openai/gpt-4.1"],
                "thinking": "off",
                "max_iterations": 25,
                "context_ratio": 0.5,
                "max_output_tokens": None,
            },
        )()
        self.agents = {
            "researcher": FakeAgentConfig(),
        }
        self.roles: dict[str, FakeRoleConfig] = roles or {}

    def agent_models(self, name: str) -> list[str]:
        a = self.agents.get(name)
        return a.models if a and a.models else self.defaults.models

    def agent_max_iterations(self, name: str) -> int:
        a = self.agents.get(name)
        return a.max_iterations if a and a.max_iterations else self.defaults.max_iterations

    def agent_thinking(self, name: str) -> str:
        a = self.agents.get(name)
        return a.thinking if a and a.thinking else self.defaults.thinking

    def agent_workspace(self, name: str) -> str:
        return f"/home/.operator/agents/{name}/workspace"

    def agent_prompt_path(self, name: str) -> Path:
        return self.base_dir / "agents" / name / "AGENT.md"

    def skills_dir(self) -> Path:
        return self.base_dir / "skills"

    def system_prompt_path(self) -> Path:
        return self.base_dir / "SYSTEM.md"

    def agent_context_ratio(self, name: str) -> float:
        a = self.agents.get(name)
        return a.context_ratio if a and a.context_ratio else self.defaults.context_ratio

    def agent_max_output_tokens(self, name: str) -> int | None:
        a = self.agents.get(name)
        return a.max_output_tokens if a and a.max_output_tokens else self.defaults.max_output_tokens

    def agent_tool_filter(self, name: str):  # noqa: ARG002
        return None

    def agent_skill_filter(self, name: str):
        if name == "researcher":
            return lambda skill: skill == "research"
        return None


def test_resolve_none_returns_current() -> None:
    current = SubagentContext(models=["m1"], max_iterations=5, workspace="/ws")
    result = _resolve_agent_context(None, current)
    assert result is current


def test_resolve_empty_string_returns_current() -> None:
    current = SubagentContext(models=["m1"], max_iterations=5, workspace="/ws")
    result = _resolve_agent_context("", current)
    assert result is current


def test_resolve_known_agent() -> None:
    config = FakeConfig()
    current = SubagentContext(
        models=["m1"],
        max_iterations=5,
        workspace="/ws",
        config=config,
        extra_tools=["t1"],
    )
    result = _resolve_agent_context("researcher", current)
    assert result.models == ["anthropic/claude-sonnet-4-6"]
    assert "researcher" in result.workspace
    assert result.max_iterations == 25
    assert result.thinking == "high"
    assert result.context_ratio == 0.5
    assert result.max_output_tokens is None
    assert result.tool_filter is None
    assert result.skill_filter is not None
    assert result.skill_filter("research") is True
    assert result.skill_filter("other") is False
    assert result.agent_name == "researcher"


def test_resolve_preserves_parent_keys() -> None:
    config = FakeConfig()
    current = SubagentContext(
        models=["m1"],
        max_iterations=5,
        workspace="/ws",
        config=config,
        extra_tools=["web_fetch"],
        usage={"prompt_tokens": 100},
        shared_dir=Path("/shared"),
    )
    result = _resolve_agent_context("researcher", current)
    assert result.extra_tools == ["web_fetch"]
    assert result.usage == {"prompt_tokens": 100}
    assert result.shared_dir == Path("/shared")
    assert result.config is config


def test_resolve_unknown_agent_raises() -> None:
    config = FakeConfig()
    current = SubagentContext(models=["m1"], max_iterations=5, workspace="/ws", config=config)
    with pytest.raises(ValueError, match="unknown agent 'ghost'"):
        _resolve_agent_context("ghost", current)


def test_resolve_without_config_returns_current() -> None:
    current = SubagentContext(models=["m1"], max_iterations=5, workspace="/ws")
    result = _resolve_agent_context("anything", current)
    assert result is current


def test_spawn_agent_without_explicit_target_uses_current_agent_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_agent(**kwargs):
        captured["kwargs"] = kwargs
        captured["system_prompt"] = kwargs["messages"][0]["content"]
        captured["user_message"] = kwargs["messages"][1]
        captured["agent_name"] = kwargs["agent_name"]
        return "done"

    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )
    monkeypatch.setattr("operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)
    set_user_context(UserContext(username="gavin", roles=["admin"]))

    memory_store = MemoryStore(base_dir=tmp_path)
    run_envelope = ChatEnvelope(
        context=MessageContext(
            platform="slack",
            channel_id="C123",
            channel_name="#general",
            user_id="slack:U123",
            user_name="Gavin",
            username="gavin",
            roles=["admin"],
            timezone="America/Vancouver",
            chat_type="channel",
        ),
        transport_prompt="# Messaging\n\nUse send_message for follow-ups.",
        is_private=True,
    )

    subagent.configure(
        SubagentContext(
            models=["openai/gpt-4.1"],
            max_iterations=5,
            workspace="/ws",
            agent_name="operator",
            skill_filter=None,
            memory_store=memory_store,
            username="gavin",
            allow_user_scope=True,
            allowed_agents={"operator"},
            base_dir=tmp_path,
            config=Config(
                defaults={"models": ["openai/gpt-4.1"]},
                agents={"operator": {}},
            ).set_base_dir(tmp_path),
            run_envelope=run_envelope,
        )
    )

    result = asyncio.run(spawn_agent("Summarize the release branch."))

    assert result == "done"
    assert captured["agent_name"] == "operator"
    assert "# Agent\n\noperator" in captured["system_prompt"]
    assert "# Messaging" in captured["system_prompt"]
    assert "# Context" in captured["system_prompt"]
    assert "- Platform: slack" in captured["system_prompt"]
    assert "- Agent (You): operator" in captured["system_prompt"]
    assert "- Username: gavin" in captured["system_prompt"]
    assert "- Workspace: `" in captured["system_prompt"]
    assert set(captured["kwargs"]) == {
        "messages",
        "models",
        "max_iterations",
        "workspace",
        "agent_name",
        "depth",
        "context_ratio",
        "max_output_tokens",
        "thinking",
        "extra_tools",
        "usage",
        "tool_filter",
        "skill_filter",
        "shared_dir",
        "config",
        "memory_store",
        "username",
        "allow_user_scope",
        "allowed_agents",
        "base_dir",
        "run_envelope",
    }
    user_message = captured["user_message"]
    assert user_message["content"] == "Summarize the release branch."
    assert user_message[MESSAGE_CREATED_AT_KEY]
    assert captured["kwargs"]["memory_store"] is memory_store
    assert captured["kwargs"]["username"] == "gavin"
    assert captured["kwargs"]["allow_user_scope"] is True
    assert captured["kwargs"]["allowed_agents"] == {"operator"}
    assert captured["kwargs"]["base_dir"] == tmp_path


def test_spawn_agent_reconfigures_memory_and_state_context_for_target_agent(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, object] = {}
    store = MemoryStore(base_dir=tmp_path)

    async def fake_run_agent(**kwargs):
        captured["kwargs"] = kwargs
        captured["memory_ctx"] = memory_tools._get_context()
        captured["state_ctx"] = state_tools._get_context()
        return "done"

    monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )
    monkeypatch.setattr("operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: [])

    config = Config(
        defaults={"models": ["openai/gpt-4.1"]},
        agents={"operator": {}, "researcher": {}},
        roles={"dev": {"agents": ["operator", "researcher"]}},
    ).set_base_dir(tmp_path)

    subagent.configure(
        SubagentContext(
            models=["openai/gpt-4.1"],
            max_iterations=5,
            workspace=str(tmp_path / "agents" / "operator" / "workspace"),
            agent_name="operator",
            skill_filter=None,
            memory_store=store,
            username="alice",
            allow_user_scope=True,
            allowed_agents={"operator", "researcher"},
            base_dir=tmp_path,
            config=config,
            run_envelope=ChatEnvelope(
                context=MessageContext(
                    platform="slack",
                    channel_id="C1",
                    channel_name="#ops",
                    user_id="slack:U1",
                    user_name="Alice",
                    username="alice",
                    roles=["dev"],
                ),
                is_private=True,
            ),
        )
    )
    set_user_context(UserContext(username="alice", roles=["dev"]))

    result = asyncio.run(spawn_agent("do research", agent="researcher"))

    assert result == "done"
    memory_ctx = captured["memory_ctx"]
    assert memory_ctx[0] is store
    assert memory_ctx[1] == "researcher"
    assert memory_ctx[2] == "alice"
    assert memory_ctx[3] is True
    assert captured["state_ctx"] == ("researcher", tmp_path)
    assert captured["kwargs"]["max_iterations"] == 25


def test_spawn_agent_preserves_job_run_mode(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    async def fake_run_agent(**kwargs):
        captured["system_prompt"] = kwargs["messages"][0]["content"]
        captured["user_message"] = kwargs["messages"][1]
        captured["agent_name"] = kwargs["agent_name"]
        return "done"

    monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )
    monkeypatch.setattr("operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: [])

    config = Config(
        defaults={"models": ["openai/gpt-4.1"]},
        agents={"operator": {}, "researcher": {}},
    ).set_base_dir(tmp_path)

    def _run_in_clean_context() -> str:
        subagent.configure(
            SubagentContext(
                models=["openai/gpt-4.1"],
                max_iterations=5,
                workspace=str(tmp_path / "agents" / "operator" / "workspace"),
                agent_name="operator",
                config=config,
                run_envelope=JobEnvelope(
                    name="nightly-sync",
                    description="Sync data",
                    schedule="0 2 * * *",
                    path=tmp_path / "jobs" / "nightly-sync" / "JOB.md",
                    prerun_output="42 rows ready",
                    transport_prompt="# Messaging\n\nUse send_message.",
                ),
            )
        )
        return asyncio.run(
            spawn_agent(
                "Summarize the synced rows.",
                context="Only report material changes.",
                agent="researcher",
            )
        )

    result = contextvars.Context().run(_run_in_clean_context)

    assert result == "done"
    assert captured["agent_name"] == "researcher"
    assert "# Job" in captured["system_prompt"]
    assert "- Name: nightly-sync" in captured["system_prompt"]
    assert "<prerun_output>" in captured["system_prompt"]
    assert "42 rows ready" in captured["system_prompt"]
    assert (
        "You are a focused sub-agent running in an ephemeral child run."
        not in captured["system_prompt"]
    )
    assert "<additional_context>" in captured["user_message"]["content"]


# --- Access control tests ---


class TestUserCanAccessAgent:
    """Unit tests for _user_can_access_agent."""

    def test_no_user_context_allows_access(self) -> None:
        """Job runs with no user context should be allowed."""
        config = FakeConfig(roles={"dev": FakeRoleConfig(agents=["researcher"])})
        # Run in a fresh context where _user_var is unset — simulates a job run
        ctx = contextvars.Context()
        assert ctx.run(_user_can_access_agent, "researcher", config) is True

    def test_admin_always_allowed(self) -> None:
        config = FakeConfig(roles={})  # no roles define "researcher"
        set_user_context(UserContext(username="boss", roles=["admin"]))
        assert _user_can_access_agent("researcher", config) is True

    def test_role_grants_access(self) -> None:
        config = FakeConfig(roles={"dev": FakeRoleConfig(agents=["researcher"])})
        set_user_context(UserContext(username="alice", roles=["dev"]))
        assert _user_can_access_agent("researcher", config) is True

    def test_role_without_agent_denies(self) -> None:
        config = FakeConfig(roles={"dev": FakeRoleConfig(agents=["coder"])})
        set_user_context(UserContext(username="alice", roles=["dev"]))
        assert _user_can_access_agent("researcher", config) is False

    def test_no_matching_role_denies(self) -> None:
        config = FakeConfig(roles={"ops": FakeRoleConfig(agents=["researcher"])})
        set_user_context(UserContext(username="alice", roles=["dev"]))
        assert _user_can_access_agent("researcher", config) is False

    def test_empty_roles_denies(self) -> None:
        config = FakeConfig(roles={})
        set_user_context(UserContext(username="alice", roles=["dev"]))
        assert _user_can_access_agent("researcher", config) is False


class TestSpawnAgentAccessControl:
    """Integration tests: spawn_agent checks user access before dispatching."""

    def _configure_subagent(self, config: FakeConfig) -> None:
        subagent.configure(
            SubagentContext(
                models=["openai/gpt-4.1"],
                max_iterations=5,
                workspace="/ws",
                agent_name="operator",
                skill_filter=None,
                config=config,
                run_envelope=JobEnvelope(
                    name="nightly-sync",
                    description="Sync data",
                    schedule="0 2 * * *",
                    path=Path("/home/.operator/jobs/nightly-sync/JOB.md"),
                ),
            )
        )

    def test_spawn_denied_for_unauthorized_user(self) -> None:
        config = FakeConfig(roles={"dev": FakeRoleConfig(agents=["coder"])})
        self._configure_subagent(config)
        set_user_context(UserContext(username="alice", roles=["dev"]))
        result = asyncio.run(spawn_agent("do research", agent="researcher"))
        assert result == "[error: you don't have access to agent 'researcher']"

    def test_spawn_allowed_for_authorized_user(self, monkeypatch) -> None:
        config = FakeConfig(roles={"dev": FakeRoleConfig(agents=["researcher"])})
        self._configure_subagent(config)
        set_user_context(UserContext(username="alice", roles=["dev"]))

        async def fake_run_agent(**kwargs):  # noqa: ARG001
            return "done"

        monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)
        monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
        monkeypatch.setattr(
            "operator_ai.prompts.load_agent_prompt",
            lambda _config, agent_name: f"# Agent\n\n{agent_name}",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_skills_prompt",
            lambda _skills_dir, **_kwargs: "",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: []
        )
        result = asyncio.run(spawn_agent("do research", agent="researcher"))
        assert result == "done"

    def test_spawn_allowed_for_admin(self, monkeypatch) -> None:
        config = FakeConfig(roles={})  # no roles grant access to researcher
        self._configure_subagent(config)
        set_user_context(UserContext(username="boss", roles=["admin"]))

        async def fake_run_agent(**kwargs):  # noqa: ARG001
            return "admin-done"

        monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)
        monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
        monkeypatch.setattr(
            "operator_ai.prompts.load_agent_prompt",
            lambda _config, agent_name: f"# Agent\n\n{agent_name}",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_skills_prompt",
            lambda _skills_dir, **_kwargs: "",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: []
        )
        result = asyncio.run(spawn_agent("do research", agent="researcher"))
        assert result == "admin-done"

    def test_spawn_no_user_context_allows(self, monkeypatch) -> None:
        """Job runs (no user context) should bypass the access check."""
        config = FakeConfig(roles={})

        async def fake_run_agent(**kwargs):  # noqa: ARG001
            return "job-done"

        monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)
        monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
        monkeypatch.setattr(
            "operator_ai.prompts.load_agent_prompt",
            lambda _config, agent_name: f"# Agent\n\n{agent_name}",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_skills_prompt",
            lambda _skills_dir, **_kwargs: "",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: []
        )

        # Run in a fresh context where _user_var is unset — simulates a job run
        def _run_in_clean_context() -> str:
            self._configure_subagent(config)
            return asyncio.run(spawn_agent("do research", agent="researcher"))

        ctx = contextvars.Context()
        result = ctx.run(_run_in_clean_context)
        assert result == "job-done"

    def test_spawn_no_agent_specified_skips_access_check(self, monkeypatch) -> None:
        """When no agent is specified (inherit current), no access check is performed."""
        config = FakeConfig(roles={})
        self._configure_subagent(config)
        # User with no roles — would fail access check if it ran
        set_user_context(UserContext(username="alice", roles=["dev"]))

        async def fake_run_agent(**kwargs):  # noqa: ARG001
            return "inherited"

        monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)
        monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
        monkeypatch.setattr(
            "operator_ai.prompts.load_agent_prompt",
            lambda _config, agent_name: f"# Agent\n\n{agent_name}",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_skills_prompt",
            lambda _skills_dir, **_kwargs: "",
        )
        monkeypatch.setattr(
            "operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: []
        )
        result = asyncio.run(spawn_agent("summarize this"))
        assert result == "inherited"
