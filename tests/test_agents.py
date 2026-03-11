"""Tests for agent frontmatter parsing and prompt injection."""

from __future__ import annotations

from pathlib import Path

from operator_ai.agents import AgentInfo, build_agents_prompt, load_agent_body, scan_agents


def test_scan_agents_with_frontmatter(tmp_path: Path) -> None:
    agent_dir = tmp_path / "researcher"
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text(
        "---\nname: researcher\ndescription: Research assistant\n---\n\nYou are a researcher."
    )
    agents = scan_agents(tmp_path)
    assert len(agents) == 1
    assert agents[0].name == "researcher"
    assert agents[0].description == "Research assistant"


def test_scan_agents_skips_no_description(tmp_path: Path) -> None:
    agent_dir = tmp_path / "empty"
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text("---\nname: empty\n---\n\nNo description.")
    agents = scan_agents(tmp_path)
    assert len(agents) == 0


def test_scan_agents_skips_no_frontmatter(tmp_path: Path) -> None:
    agent_dir = tmp_path / "plain"
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text("Just a plain agent prompt.")
    agents = scan_agents(tmp_path)
    assert len(agents) == 0


def test_scan_agents_uses_dir_name_as_fallback(tmp_path: Path) -> None:
    agent_dir = tmp_path / "coder"
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text("---\ndescription: Writes code\n---\n\nYou write code.")
    agents = scan_agents(tmp_path)
    assert len(agents) == 1
    assert agents[0].name == "coder"


def test_scan_agents_nonexistent_dir(tmp_path: Path) -> None:
    agents = scan_agents(tmp_path / "does_not_exist")
    assert agents == []


def test_scan_agents_skips_files(tmp_path: Path) -> None:
    (tmp_path / "not-a-dir.md").write_text("file, not directory")
    agents = scan_agents(tmp_path)
    assert agents == []


def test_scan_agents_skips_malformed_yaml(tmp_path: Path) -> None:
    agent_dir = tmp_path / "broken"
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text("---\n: [invalid yaml\n---\n\nBody.")
    agents = scan_agents(tmp_path)
    assert agents == []


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
    # Both beta and gamma should be inaccessible
    lines = prompt.strip().split("\n")
    agent_lines = [line for line in lines if line.startswith("- **")]
    assert len(agent_lines) == 2
    assert all("inaccessible" in line for line in agent_lines)
