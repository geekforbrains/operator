from __future__ import annotations

import asyncio
from pathlib import Path

from operator_ai.config import Config
from operator_ai.jobs import Job, _build_job_prompt, _execute_job, _job_memory_scopes
from operator_ai.message_timestamps import MESSAGE_CREATED_AT_KEY
from operator_ai.store import JobState
from operator_ai.tools import memory as memory_tools
from operator_ai.tools.context import get_skill_filter


class FakeMemoryStore:
    def get_pinned_memories(self, scope: str, scope_id: str) -> list[dict[str, str]]:
        if (scope, scope_id) == ("agent", "operator"):
            return [{"scope": "agent", "content": "Use terse status updates."}]
        return []

    async def search(
        self,
        query: str,
        scopes: list[tuple[str, str]],
        top_k: int | None = None,
    ) -> list[dict[str, str]]:
        del top_k
        assert scopes == [("agent", "operator"), ("global", "global")]
        if query == "Summarize the latest incidents.":
            return [{"content": "The team prefers a short summary with links."}]
        return []


class FakeStore:
    def __init__(self) -> None:
        self.state = JobState()

    def load_job_state(self, _name: str) -> JobState:
        return self.state

    def save_job_state(self, _name: str, state: JobState) -> None:
        self.state = state

    def ensure_conversation(self, **_kwargs) -> None:
        return None

    def append_messages(self, _conversation_id: str, _messages: list[dict]) -> None:
        return None


def _config() -> Config:
    return Config(
        runtime={"timezone": "America/Vancouver"},
        defaults={"models": ["test/model"]},
        agents={"operator": {}},
    )


def test_job_memory_scopes_include_agent_and_global() -> None:
    assert _job_memory_scopes("operator") == [("agent", "operator"), ("global", "global")]


def test_build_job_prompt_includes_job_memory_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )
    monkeypatch.setattr("operator_ai.prompts.scan_agents", lambda: [])

    job = Job(
        name="incident-digest",
        description="Summarize incidents",
        schedule="0 8 * * *",
        prompt="Summarize the latest incidents.",
        job_dir=tmp_path,
    )

    prompt = asyncio.run(
        _build_job_prompt(
            config=_config(),
            job=job,
            agent_name="operator",
            prerun_output="",
            transport=None,
            memory_store=FakeMemoryStore(),
        )
    )

    assert "# Pinned Memories" in prompt
    assert "Use terse status updates." in prompt
    assert '<context_snapshot source="memories">' in prompt
    assert "Relevant memories from previous work:" in prompt
    assert "The team prefers a short summary with links." in prompt


def test_execute_job_configures_memory_and_skill_filter(monkeypatch, tmp_path: Path) -> None:
    async def fake_run_agent(**_kwargs) -> str:
        skill_filter = get_skill_filter()
        assert skill_filter is not None
        assert skill_filter("allowed-skill") is True
        assert skill_filter("blocked-skill") is False
        assert await memory_tools.search_memories("status") == "No relevant memories found."
        user_message = _kwargs["messages"][1]
        assert user_message[MESSAGE_CREATED_AT_KEY]
        return "done"

    monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)

    job = Job(
        name="incident-digest",
        description="Summarize incidents",
        schedule="0 8 * * *",
        prompt="Summarize the latest incidents.",
        job_dir=tmp_path,
    )
    config = Config(
        runtime={"timezone": "America/Vancouver"},
        defaults={"models": ["test/model"]},
        agents={"operator": {"permissions": {"skills": ["allowed-skill"]}}},
    )
    store = FakeStore()

    asyncio.run(
        _execute_job(
            job,
            config,
            transports={},
            store=store,
            memory_store=FakeMemoryStore(),
        )
    )

    assert store.state.last_result == "success"
