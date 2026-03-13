"""Tests for configured-agent metadata and prompt injection."""

from __future__ import annotations

from pathlib import Path

from operator_ai.agent import (
    AgentInfo,
    build_agents_prompt,
    load_agent_body,
    load_agent_info,
    load_configured_agents,
)
from operator_ai.config import Config


def _config(tmp_path: Path, agents: dict[str, dict] | None = None) -> Config:
    config = Config(
        defaults={"models": ["test/model"]},
        agents=agents or {"operator": {}, "researcher": {}},
    )
    config.set_base_dir(tmp_path)
    return config


def test_load_agent_info_uses_configured_name(tmp_path: Path) -> None:
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text(
        "---\nname: mismatched\ndescription: Research assistant\n---\n\nYou are a researcher."
    )

    info = load_agent_info(agent_md, agent_name="researcher")

    assert info == AgentInfo(name="researcher", description="Research assistant")


def test_load_agent_info_missing_description_uses_placeholder(tmp_path: Path) -> None:
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("---\nname: researcher\n---\n\nNo description.")

    info = load_agent_info(agent_md, agent_name="researcher")

    assert info == AgentInfo(name="researcher", description="No description provided.")


def test_load_agent_info_missing_file_uses_placeholder(tmp_path: Path) -> None:
    info = load_agent_info(tmp_path / "missing.md", agent_name="researcher")

    assert info == AgentInfo(name="researcher", description="No description provided.")


def test_load_agent_info_malformed_yaml_uses_placeholder(tmp_path: Path) -> None:
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("---\n: [invalid yaml\n---\n\nBody.")

    info = load_agent_info(agent_md, agent_name="researcher")

    assert info == AgentInfo(name="researcher", description="No description provided.")


def test_load_configured_agents_uses_config_order(tmp_path: Path) -> None:
    config = _config(tmp_path, {"operator": {}, "reviewer": {}, "researcher": {}})
    for name, description in {
        "operator": "Default agent",
        "reviewer": "Reviews changes",
        "researcher": "Does research",
    }.items():
        agent_dir = tmp_path / "agents" / name
        agent_dir.mkdir(parents=True)
        (agent_dir / "AGENT.md").write_text(f"---\ndescription: {description}\n---\n\n{name}")

    infos = load_configured_agents(config)

    assert infos == [
        AgentInfo(name="operator", description="Default agent"),
        AgentInfo(name="reviewer", description="Reviews changes"),
        AgentInfo(name="researcher", description="Does research"),
    ]


def test_load_configured_agents_keeps_configured_agent_without_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path, {"operator": {}, "ghost": {}})
    operator_dir = tmp_path / "agents" / "operator"
    operator_dir.mkdir(parents=True)
    (operator_dir / "AGENT.md").write_text("---\ndescription: Default agent\n---\n\noperator")

    infos = load_configured_agents(config)

    assert infos == [
        AgentInfo(name="operator", description="Default agent"),
        AgentInfo(name="ghost", description="No description provided."),
    ]


def test_load_agent_body_strips_frontmatter(tmp_path: Path) -> None:
    md = tmp_path / "AGENT.md"
    md.write_text("---\nname: test\ndescription: Test agent\n---\n\nHello world.")
    body = load_agent_body(md)
    assert body == "Hello world."
    assert "---" not in body


def test_load_agent_body_no_frontmatter(tmp_path: Path) -> None:
    md = tmp_path / "AGENT.md"
    md.write_text("Just a plain prompt.")
    body = load_agent_body(md)
    assert body == "Just a plain prompt."


def test_load_agent_body_missing_file(tmp_path: Path) -> None:
    body = load_agent_body(tmp_path / "nonexistent.md")
    assert body == ""


def test_load_agent_body_frontmatter_only(tmp_path: Path) -> None:
    md = tmp_path / "AGENT.md"
    md.write_text("---\nname: test\ndescription: Test\n---\n")
    body = load_agent_body(md)
    assert body == ""


def test_build_agents_prompt_excludes_current() -> None:
    agents = [
        AgentInfo(name="alpha", description="Alpha agent"),
        AgentInfo(name="beta", description="Beta agent"),
    ]
    prompt = build_agents_prompt(agents, "alpha")
    assert "**alpha**" not in prompt
    assert "**beta**" in prompt
    assert "Beta agent" in prompt


def test_build_agents_prompt_empty_when_only_current() -> None:
    agents = [AgentInfo(name="solo", description="Only agent")]
    prompt = build_agents_prompt(agents, "solo")
    assert prompt == ""


def test_build_agents_prompt_empty_when_no_agents() -> None:
    prompt = build_agents_prompt([], "any")
    assert prompt == ""


def test_build_agents_prompt_all_accessible_when_allowed_none() -> None:
    """Admin case: allowed_agents=None means all agents are accessible."""
    agents = [
        AgentInfo(name="alpha", description="Alpha agent"),
        AgentInfo(name="beta", description="Beta agent"),
        AgentInfo(name="gamma", description="Gamma agent"),
    ]
    prompt = build_agents_prompt(agents, "alpha", allowed_agents=None)
    assert "inaccessible" not in prompt
    assert "**beta**" in prompt
    assert "**gamma**" in prompt


def test_build_agents_prompt_inaccessible_annotation() -> None:
    """Agents not in allowed_agents are annotated as inaccessible."""
    agents = [
        AgentInfo(name="alpha", description="Alpha agent"),
        AgentInfo(name="beta", description="Beta agent"),
        AgentInfo(name="gamma", description="Gamma agent"),
    ]
    prompt = build_agents_prompt(agents, "alpha", allowed_agents={"beta"})
    assert "**beta**: Beta agent" in prompt
    assert "inaccessible" not in prompt.split("beta")[1].split("\n")[0]
    assert "**gamma**: Gamma agent *(inaccessible to current user)*" in prompt


def test_build_agents_prompt_allowed_agents_shown_normally() -> None:
    """Agents in allowed_agents are shown without annotation."""
    agents = [
        AgentInfo(name="alpha", description="Alpha agent"),
        AgentInfo(name="beta", description="Beta agent"),
    ]
    prompt = build_agents_prompt(agents, "alpha", allowed_agents={"beta"})
    assert "- **beta**: Beta agent" in prompt
    assert "inaccessible" not in prompt


def test_build_agents_prompt_current_excluded_with_allowed() -> None:
    """Current agent is still excluded even when allowed_agents is provided."""
    agents = [
        AgentInfo(name="alpha", description="Alpha agent"),
        AgentInfo(name="beta", description="Beta agent"),
    ]
    prompt = build_agents_prompt(agents, "alpha", allowed_agents={"alpha", "beta"})
    assert "**alpha**" not in prompt
    assert "**beta**" in prompt


def test_build_agents_prompt_empty_allowed_marks_all_inaccessible() -> None:
    """Empty allowed_agents set marks all other agents as inaccessible."""
    agents = [
        AgentInfo(name="alpha", description="Alpha agent"),
        AgentInfo(name="beta", description="Beta agent"),
        AgentInfo(name="gamma", description="Gamma agent"),
    ]
    prompt = build_agents_prompt(agents, "alpha", allowed_agents=set())
    assert "*(inaccessible to current user)*" in prompt
    agent_lines = [line for line in prompt.strip().split("\n") if line.startswith("- **")]
    assert len(agent_lines) == 2
    assert all("inaccessible" in line for line in agent_lines)
