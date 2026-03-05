"""Agent metadata discovery — parse AGENT.md frontmatter for inter-agent awareness."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from operator_ai.config import OPERATOR_DIR
from operator_ai.skills import extract_body, parse_frontmatter

logger = logging.getLogger("operator.agents")

AGENTS_DIR = OPERATOR_DIR / "agents"


@dataclass
class AgentInfo:
    name: str
    description: str


def scan_agents(agents_dir: Path = AGENTS_DIR) -> list[AgentInfo]:
    """Scan agents/*/AGENT.md for frontmatter with name and description."""
    agents: list[AgentInfo] = []
    if not agents_dir.is_dir():
        return agents

    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent_md = agent_dir / "AGENT.md"
        if not agent_md.exists():
            continue
        try:
            fm = parse_frontmatter(agent_md.read_text())
            if not fm:
                continue
            name = fm.get("name", agent_dir.name)
            description = fm.get("description", "")
            if not description:
                continue
            agents.append(AgentInfo(name=name, description=description))
        except Exception as e:
            logger.warning("Failed to parse %s: %s", agent_md, e)
    return agents


def load_agent_body(agent_md: Path) -> str:
    """Load the markdown body of an AGENT.md, stripping frontmatter if present."""
    if not agent_md.exists():
        return ""
    text = agent_md.read_text()
    if parse_frontmatter(text) is not None:
        return extract_body(text)
    return text.strip()


def build_agents_prompt(agents: list[AgentInfo], current_agent: str) -> str:
    """Build markdown block listing other agents for system prompt injection."""
    others = [a for a in agents if a.name != current_agent]
    if not others:
        return ""
    lines = [
        "# Available Agents",
        "",
        "You can delegate tasks to these agents using `spawn_agent`:",
    ]
    for a in others:
        lines.append(f"- **{a.name}**: {a.description}")
    return "\n".join(lines)
