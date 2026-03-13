from __future__ import annotations

from pathlib import Path

from operator_ai.agent import AgentInfo
from operator_ai.config import Config
from operator_ai.memory import MemoryStore
from operator_ai.prompts import CACHE_BOUNDARY, assemble_system_prompt
from operator_ai.run_prompt import ChatEnvelope, JobEnvelope, build_agent_system_prompt
from operator_ai.transport.base import MessageContext


def _make_config(tmp_path: Path | None) -> Config:
    config = Config(
        defaults={"models": ["test/model"]},
        agents={"operator": {}},
    )
    if tmp_path is not None:
        config.set_base_dir(tmp_path)
    return config


def _stub_prompts(monkeypatch: object) -> None:
    """Replace filesystem-dependent prompt loaders with stubs."""
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )


# ── Prompt ordering ──────────────────────────────────────────────


def test_prompt_ordering_system_then_agent(monkeypatch) -> None:
    _stub_prompts(monkeypatch)
    prompt = assemble_system_prompt(
        config=_make_config(None),
        agent_name="operator",
        available_agents=[],
    )
    assert prompt.startswith("# System\n\n# Agent\n\noperator")


def test_skills_appear_in_stable_prefix(monkeypatch) -> None:
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "# Available Skills\n\n- **research**: Do research",
    )

    prompt = assemble_system_prompt(
        config=_make_config(None),
        agent_name="operator",
        available_agents=[],
    )
    # Skills should be in the stable prefix (before cache boundary or in the whole prompt)
    if CACHE_BOUNDARY in prompt:
        stable, _ = prompt.split(CACHE_BOUNDARY, 1)
    else:
        stable = prompt
    assert "# Available Skills" in stable


def test_agents_appear_in_stable_prefix(monkeypatch) -> None:
    _stub_prompts(monkeypatch)
    agents = [
        AgentInfo(name="researcher", description="Does research"),
        AgentInfo(name="operator", description="Default agent"),
    ]
    prompt = assemble_system_prompt(
        config=_make_config(None),
        agent_name="operator",
        available_agents=agents,
    )
    if CACHE_BOUNDARY in prompt:
        stable, _ = prompt.split(CACHE_BOUNDARY, 1)
    else:
        stable = prompt
    assert "# Available Agents" in stable
    assert "researcher" in stable
    # The current agent should not appear in the "other agents" list
    assert "**operator**" not in stable


# ── Cache boundary ───────────────────────────────────────────────


def test_cache_boundary_separates_stable_and_dynamic(monkeypatch) -> None:
    _stub_prompts(monkeypatch)
    prompt = assemble_system_prompt(
        config=_make_config(None),
        agent_name="operator",
        transport_extra="# Slack\n\nYou are in Slack.",
        available_agents=[],
    )
    assert CACHE_BOUNDARY in prompt
    stable, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert "# System" in stable
    assert "# Agent" in stable
    assert "# Slack" in dynamic


def test_no_cache_boundary_when_no_dynamic_content(monkeypatch) -> None:
    _stub_prompts(monkeypatch)
    prompt = assemble_system_prompt(
        config=_make_config(None),
        agent_name="operator",
        available_agents=[],
    )
    assert CACHE_BOUNDARY not in prompt


# ── Transport extra ──────────────────────────────────────────────


def test_transport_extra_in_dynamic_suffix(monkeypatch) -> None:
    _stub_prompts(monkeypatch)
    prompt = assemble_system_prompt(
        config=_make_config(None),
        agent_name="operator",
        transport_extra="# Transport Context\n\nChannel: #general",
        available_agents=[],
    )
    _, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert "# Transport Context" in dynamic
    assert "Channel: #general" in dynamic


# ── Rule injection from memory store ─────────────────────────────


def test_global_rules_injected(monkeypatch, tmp_path) -> None:
    _stub_prompts(monkeypatch)
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("global", "response-style", "Be concise")
    store.upsert_rule("global", "tooling-preference", "Use uv over pip")

    prompt = assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        available_agents=[],
    )
    assert CACHE_BOUNDARY in prompt
    _, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert "# Rules" in dynamic
    assert "## Global Rules" in dynamic
    assert "Be concise" in dynamic
    assert "Use uv over pip" in dynamic


def test_agent_rules_injected(monkeypatch, tmp_path) -> None:
    _stub_prompts(monkeypatch)
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("agent:operator", "pre-commit-checks", "Always check tests before committing")

    prompt = assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        available_agents=[],
    )
    _, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert "## Agent Rules" in dynamic
    assert "Always check tests before committing" in dynamic


def test_user_rules_injected_when_private(monkeypatch, tmp_path) -> None:
    _stub_prompts(monkeypatch)
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("user:gavin", "response-depth", "Prefer verbose output")

    prompt = assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        username="gavin",
        is_private=True,
        available_agents=[],
    )
    _, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert "## User Rules" in dynamic
    assert "Prefer verbose output" in dynamic


def test_user_rules_not_injected_when_not_private(monkeypatch, tmp_path) -> None:
    _stub_prompts(monkeypatch)
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("user:gavin", "response-depth", "Prefer verbose output")

    prompt = assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        username="gavin",
        is_private=False,
        available_agents=[],
    )
    if CACHE_BOUNDARY in prompt:
        _, dynamic = prompt.split(CACHE_BOUNDARY, 1)
        assert "## User Rules" not in dynamic
    else:
        assert "## User Rules" not in prompt


def test_all_rule_scopes_in_order(monkeypatch, tmp_path) -> None:
    _stub_prompts(monkeypatch)
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("global", "global-rule", "Global rule")
    store.upsert_rule("agent:operator", "agent-rule", "Agent rule")
    store.upsert_rule("user:gavin", "user-rule", "User rule")

    prompt = assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        username="gavin",
        is_private=True,
        available_agents=[],
    )
    _, dynamic = prompt.split(CACHE_BOUNDARY, 1)

    # Verify ordering: global before agent before user
    global_pos = dynamic.index("## Global Rules")
    agent_pos = dynamic.index("## Agent Rules")
    user_pos = dynamic.index("## User Rules")
    assert global_pos < agent_pos < user_pos


def test_rules_after_transport_extra(monkeypatch, tmp_path) -> None:
    _stub_prompts(monkeypatch)
    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("global", "helpfulness", "Be helpful")

    prompt = assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        transport_extra="# Transport\n\nSlack context",
        available_agents=[],
    )
    _, dynamic = prompt.split(CACHE_BOUNDARY, 1)

    transport_pos = dynamic.index("# Transport")
    rules_pos = dynamic.index("# Rules")
    assert transport_pos < rules_pos


# ── Empty rules ──────────────────────────────────────────────────


def test_no_rules_section_when_no_rules(monkeypatch, tmp_path) -> None:
    _stub_prompts(monkeypatch)
    store = MemoryStore(base_dir=tmp_path)
    # No rules created

    prompt = assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        available_agents=[],
    )
    assert "# Rules" not in prompt


def test_no_rules_section_without_memory_store(monkeypatch) -> None:
    _stub_prompts(monkeypatch)
    prompt = assemble_system_prompt(
        config=_make_config(None),
        agent_name="operator",
        memory_store=None,
        available_agents=[],
    )
    assert "# Rules" not in prompt


# ── Skills filtering ─────────────────────────────────────────────


def test_skill_filter_passed_through(monkeypatch, tmp_path) -> None:
    """Verify skill_filter kwarg reaches the skills prompt loader."""
    captured = {}

    def fake_load_skills(skills_dir, *, skill_filter=None):  # noqa: ARG001
        captured["skill_filter"] = skill_filter
        return ""

    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr("operator_ai.prompts.load_skills_prompt", fake_load_skills)

    only_research = lambda name: name == "research"  # noqa: E731
    assemble_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        skill_filter=only_research,
        available_agents=[],
    )
    assert captured["skill_filter"] is only_research


# ── Shared run-envelope prompt assembly ──────────────────────────


def test_build_agent_system_prompt_renders_chat_envelope(monkeypatch, tmp_path: Path) -> None:
    _stub_prompts(monkeypatch)
    monkeypatch.setattr("operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: [])

    prompt = build_agent_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        username="gavin",
        skill_filter=_make_config(tmp_path).agent_skill_filter("operator"),
        run_envelope=ChatEnvelope(
            context=MessageContext(
                platform="slack",
                channel_id="C123",
                channel_name="#general",
                user_id="slack:U123",
                user_name="Gavin",
                username="gavin",
                roles=["admin", "developer"],
                timezone="America/Vancouver",
                chat_type="channel",
            ),
            transport_prompt="# Messaging\n\nUse send_message.",
            is_private=True,
        ),
    )

    stable, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert stable.startswith("# System\n\n# Agent\n\noperator")
    assert "# Messaging" in dynamic
    assert "# Context" in dynamic
    assert "- Platform: slack" in dynamic
    assert "- Agent (You): operator" in dynamic
    assert "- Roles: admin, developer" in dynamic
    assert "- Timezone: America/Vancouver" in dynamic


def test_build_agent_system_prompt_renders_job_envelope(monkeypatch, tmp_path: Path) -> None:
    _stub_prompts(monkeypatch)
    monkeypatch.setattr("operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: [])

    prompt = build_agent_system_prompt(
        config=_make_config(tmp_path),
        agent_name="operator",
        skill_filter=_make_config(tmp_path).agent_skill_filter("operator"),
        run_envelope=JobEnvelope(
            name="nightly-sync",
            description="Sync data",
            schedule="0 2 * * *",
            path=tmp_path / "jobs" / "nightly-sync" / "JOB.md",
            prerun_output="42 rows ready",
            transport_prompt="# Messaging\n\nUse send_message.",
        ),
    )

    stable, dynamic = prompt.split(CACHE_BOUNDARY, 1)
    assert stable.startswith("# System\n\n# Agent\n\noperator")
    assert "# Job" in dynamic
    assert "- Name: nightly-sync" in dynamic
    assert "# Messaging" in dynamic
    assert "<prerun_output>" in dynamic
    assert "42 rows ready" in dynamic
