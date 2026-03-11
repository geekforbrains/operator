"""Shared prompt assembly helpers for chat and job system prompts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from operator_ai.agents import AgentInfo, build_agents_prompt, load_agent_body, scan_agents
from operator_ai.config import OPERATOR_DIR, SKILLS_DIR, Config
from operator_ai.memory import MemoryStore
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


def _build_rules_section(
    memory_store: MemoryStore,
    agent_name: str,
    *,
    username: str = "",
    is_private: bool = False,
) -> str:
    """Build the rules section by reading all applicable rule files.

    Rules are always injected at prompt assembly time. Every file in every
    applicable rules/ directory is read and concatenated.
    """
    sections: list[str] = []

    # Global rules
    global_rules = memory_store.list_rules("global")
    if global_rules:
        lines = ["## Global Rules"]
        for mf in global_rules:
            lines.append(f"- {mf.content}")
        sections.append("\n".join(lines))

    # Agent rules
    agent_rules = memory_store.list_rules(f"agent:{agent_name}")
    if agent_rules:
        lines = ["## Agent Rules"]
        for mf in agent_rules:
            lines.append(f"- {mf.content}")
        sections.append("\n".join(lines))

    # User rules (only in private/scoped conversations)
    if username and is_private:
        user_rules = memory_store.list_rules(f"user:{username}")
        if user_rules:
            lines = ["## User Rules"]
            for mf in user_rules:
                lines.append(f"- {mf.content}")
            sections.append("\n".join(lines))

    if not sections:
        return ""

    return "# Rules\n\n" + "\n\n".join(sections)


def assemble_system_prompt(
    config: Config,
    agent_name: str,
    *,
    memory_store: MemoryStore | None = None,
    username: str = "",
    is_private: bool = False,
    transport_extra: str = "",
    skills_dir: Path = SKILLS_DIR,
    skill_filter: Callable[[str], bool] | None = None,
    available_agents: list[AgentInfo] | None = None,
) -> str:
    """Assemble the runtime system prompt with shared ordering for chat and jobs.

    Content is split into a stable prefix (SYSTEM.md, AGENT.md, skills, agents)
    and a dynamic suffix (transport context, rules) separated by CACHE_BOUNDARY.
    The agent layer uses this boundary to apply Anthropic prompt-cache breakpoints
    so the stable prefix is cached across turns.

    Prompt ordering (per PRINCIPLES.md):
      1. SYSTEM.md
      2. AGENT.md (body only, frontmatter stripped)
      3. Available tools (filtered by permissions) — handled by caller
      4. Discovered skills (filtered by permissions)
      5. Known agents (name + description)
      6. Transport-specific prompt content
      7. Global rules
      8. Agent rules
      9. User rules (when private/scoped)
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

    if transport_extra.strip():
        dynamic.append(transport_extra.strip())

    # Rules: always injected from files
    if memory_store is not None:
        rules_section = _build_rules_section(
            memory_store,
            agent_name,
            username=username,
            is_private=is_private,
        )
        if rules_section:
            dynamic.append(rules_section)

    stable_text = "\n\n".join(part for part in stable if part)
    dynamic_text = "\n\n".join(part for part in dynamic if part)

    if dynamic_text:
        return stable_text + CACHE_BOUNDARY + dynamic_text
    return stable_text
