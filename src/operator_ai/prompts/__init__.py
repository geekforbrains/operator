"""Shared prompt assembly helpers for chat and job system prompts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from operator_ai.agents import AgentInfo, build_agents_prompt, load_agent_body, scan_agents
from operator_ai.config import OPERATOR_DIR, SKILLS_DIR, Config
from operator_ai.skills import SkillInfo, build_skills_prompt, scan_skills

PROMPTS_DIR = Path(__file__).parent
SYSTEM_PROMPT_PATH = OPERATOR_DIR / "SYSTEM.md"

# Sentinel that separates the stable (cacheable) prefix from dynamic content.
# Must survive DB round-trips (stored in JSON as part of the system message).
CACHE_BOUNDARY = "\n\n<!-- cache-boundary -->\n\n"


def load_prompt(name: str) -> str:
    """Load a bundled prompt template from the prompts/ package directory."""
    path = PROMPTS_DIR / name
    return path.read_text().strip()


def load_system_prompt() -> str:
    """Load SYSTEM.md from disk, creating it from the bundled default if missing."""
    if not SYSTEM_PROMPT_PATH.exists():
        SYSTEM_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYSTEM_PROMPT_PATH.write_text(load_prompt("system.md"))
    return SYSTEM_PROMPT_PATH.read_text().strip()


def load_agent_prompt(config: Config, agent_name: str) -> str:
    """Load AGENT.md body, stripping frontmatter if present."""
    return load_agent_body(config.agent_prompt_path(agent_name))


def load_skills_prompt(
    skills_dir: Path = SKILLS_DIR,
    skill_filter: Callable[[str], bool] | None = None,
) -> str:
    """Load available-skill metadata as markdown for system prompt injection."""
    skills: list[SkillInfo] = scan_skills(skills_dir)
    if skill_filter is not None:
        skills = [s for s in skills if skill_filter(s.name)]
    return build_skills_prompt(skills)


def assemble_system_prompt(
    config: Config,
    agent_name: str,
    context_sections: Iterable[str] = (),
    *,
    pinned_memory_lines: Iterable[str] = (),
    transport_extra: str = "",
    skills_dir: Path = SKILLS_DIR,
    skill_filter: Callable[[str], bool] | None = None,
    available_agents: list[AgentInfo] | None = None,
) -> str:
    """Assemble the runtime system prompt with shared ordering for chat and jobs.

    Content is split into a stable prefix (SYSTEM.md, AGENT.md, skills) and a
    dynamic suffix (context, pinned memories, transport extras) separated by
    CACHE_BOUNDARY.  The agent layer uses this boundary to apply Anthropic
    prompt-cache breakpoints so the stable prefix is cached across turns.
    """
    # --- Stable prefix (rarely changes, safe to cache) ---
    stable: list[str] = [
        load_system_prompt(),
        load_agent_prompt(config, agent_name),
    ]

    skills_prompt = load_skills_prompt(skills_dir, skill_filter=skill_filter)
    if skills_prompt:
        stable.append(skills_prompt)

    if available_agents is None:
        available_agents = scan_agents()
    agents_prompt = build_agents_prompt(available_agents, agent_name)
    if agents_prompt:
        stable.append(agents_prompt)

    # --- Dynamic suffix (changes per conversation / turn) ---
    dynamic: list[str] = []

    now = datetime.now(config.tz)
    dynamic.append(
        f"Current time: {now.strftime('%Y-%m-%d %H:%M %Z')} ({config.defaults.timezone})"
    )

    dynamic.extend(section.strip() for section in context_sections if section and section.strip())

    pinned_lines = [line for line in pinned_memory_lines if line]
    if pinned_lines:
        dynamic.append("# Pinned Memories\n\n" + "\n".join(pinned_lines))

    if transport_extra.strip():
        dynamic.append(transport_extra.strip())

    stable_text = "\n\n".join(part for part in stable if part)
    dynamic_text = "\n\n".join(part for part in dynamic if part)

    if dynamic_text:
        return stable_text + CACHE_BOUNDARY + dynamic_text
    return stable_text
