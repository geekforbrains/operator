"""Configured agent metadata helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from operator_ai.config import Config
from operator_ai.frontmatter import extract_body, parse_frontmatter

logger = logging.getLogger("operator.agents")


@dataclass
class AgentInfo:
    name: str
    description: str


_MISSING_DESCRIPTION = "No description provided."


def load_configured_agents(config: Config) -> list[AgentInfo]:
    """Return prompt metadata for configured agents.

    The configured agent key is the runtime identity and source of truth.
    `AGENT.md` contributes human-authored description text when available.
    """
    return [
        load_agent_info(config.agent_prompt_path(name), agent_name=name) for name in config.agents
    ]


def load_agent_info(agent_md: Path, *, agent_name: str) -> AgentInfo:
    """Load prompt metadata for a configured agent.

    If `AGENT.md` is missing, malformed, or omits `description`, the agent is
    still returned so the prompt surface stays aligned with `config.agents`.
    """
    description = _MISSING_DESCRIPTION
    if not agent_md.exists():
        logger.warning("Configured agent '%s' is missing %s", agent_name, agent_md)
        return AgentInfo(name=agent_name, description=description)

    try:
        frontmatter = parse_frontmatter(agent_md.read_text()) or {}
    except Exception as e:
        logger.warning("Failed to parse %s: %s", agent_md, e)
        return AgentInfo(name=agent_name, description=description)

    prompt_name = frontmatter.get("name", "")
    if isinstance(prompt_name, str) and prompt_name and prompt_name != agent_name:
        logger.warning(
            "Configured agent '%s' has mismatched frontmatter name %r in %s",
            agent_name,
            prompt_name,
            agent_md,
        )

    prompt_description = frontmatter.get("description", "")
    if isinstance(prompt_description, str) and prompt_description.strip():
        description = prompt_description.strip()
    else:
        logger.warning("Configured agent '%s' is missing description in %s", agent_name, agent_md)

    return AgentInfo(name=agent_name, description=description)


def load_agent_body(agent_md: Path) -> str:
    """Load the markdown body of an AGENT.md, stripping frontmatter if present."""
    if not agent_md.exists():
        return ""
    return extract_body(agent_md.read_text())


def build_agents_prompt(
    agents: list[AgentInfo],
    current_agent: str,
    allowed_agents: set[str] | None = None,
) -> str:
    """Build markdown block listing other agents for system prompt injection.

    When allowed_agents is None (admin), all agents are accessible.
    When allowed_agents is a set, agents not in the set are annotated as inaccessible.
    """
    others = [a for a in agents if a.name != current_agent]
    if not others:
        return ""
    lines = [
        "# Available Agents",
        "",
        "You can delegate tasks to these agents using `spawn_agent`:",
    ]
    for a in others:
        if allowed_agents is not None and a.name not in allowed_agents:
            lines.append(f"- **{a.name}**: {a.description} *(inaccessible to current user)*")
        else:
            lines.append(f"- **{a.name}**: {a.description}")
    return "\n".join(lines)
