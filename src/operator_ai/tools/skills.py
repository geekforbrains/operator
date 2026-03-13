"""Deterministic skill management tools.

Each tool has explicit typed parameters so the agent never composes raw
YAML frontmatter.  The tools assemble the skill file internally.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from operator_ai.config import OPERATOR_DIR
from operator_ai.skills import build_skill_file, scan_skills, validate_skill_name
from operator_ai.tools.context import get_base_dir, get_skill_filter
from operator_ai.tools.registry import safe_name, tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skills_dir() -> Path:
    """Return the resolved skills directory from tool context or fallback."""
    base = get_base_dir()
    return (base or OPERATOR_DIR) / "skills"


def _parse_env(env: str) -> list[str]:
    """Parse comma-separated env var names into a list."""
    if not env:
        return []
    return [v.strip() for v in env.split(",") if v.strip()]


def _validate_fields(name: str, description: str, instructions: str) -> str | None:
    """Validate common skill fields. Returns error string or None."""
    try:
        safe_name(name, "skill")
    except ValueError as e:
        return f"[error: {e}]"

    err = validate_skill_name(name)
    if err:
        return f"[error: {err}]"
    if not description:
        return "[error: description is required]"
    if len(description) > 1024:
        return f"[error: description must be <= 1024 characters, got {len(description)}]"
    if not instructions.strip():
        return "[error: instructions must not be empty]"
    return None


def _body_warning(instructions: str) -> str:
    line_count = len(instructions.strip().splitlines())
    if line_count > 500:
        return (
            f"\n[warning: instructions is {line_count} lines — recommended max is 500. "
            "Consider splitting into references/ files.]"
        )
    return ""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    description=(
        "Create a new skill. The tool assembles the SKILL.md file — just provide the fields."
    ),
)
async def create_skill(
    name: str,
    description: str,
    instructions: str,
    env: str = "",
) -> str:
    """Create a skill.

    Args:
        name: Skill slug (lowercase alphanumeric + hyphens, 1-64 chars, no leading/trailing hyphens).
        description: What the skill does and when to use it (1-1024 chars, third person).
        instructions: Markdown body with the skill's instructions. Focus on unique knowledge the agent needs.
        env: Comma-separated environment variable names the skill requires (e.g. "GITHUB_TOKEN,SLACK_WEBHOOK_URL").
    """
    err = _validate_fields(name, description, instructions)
    if err:
        return err

    skill_dir = _skills_dir() / name
    if skill_dir.exists():
        return f"[error: skill '{name}' already exists. Use update_skill to modify.]"

    env_list = _parse_env(env)
    content = build_skill_file(
        name=name,
        description=description,
        instructions=instructions.strip(),
        env=env_list or None,
    )

    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)

    return f"Created skill '{name}' at {skill_dir}{_body_warning(instructions)}"


@tool(
    description=(
        "Update an existing skill. Replaces the SKILL.md with new values. "
        "All fields are re-specified to keep the file consistent."
    ),
)
async def update_skill(
    name: str,
    description: str,
    instructions: str,
    env: str = "",
) -> str:
    """Update a skill (full replace).

    Args:
        name: Skill slug to update.
        description: What the skill does and when to use it (1-1024 chars, third person).
        instructions: Markdown body with the skill's instructions.
        env: Comma-separated environment variable names the skill requires.
    """
    err = _validate_fields(name, description, instructions)
    if err:
        return err

    skill_dir = _skills_dir() / name
    if not skill_dir.exists():
        return f"[error: skill '{name}' not found]"

    env_list = _parse_env(env)
    content = build_skill_file(
        name=name,
        description=description,
        instructions=instructions.strip(),
        env=env_list or None,
    )

    (skill_dir / "SKILL.md").write_text(content)

    return f"Updated skill '{name}'{_body_warning(instructions)}"


@tool(description="Delete a skill and its entire directory.")
async def delete_skill(name: str) -> str:
    """Delete a skill.

    Args:
        name: Skill slug to delete.
    """
    if not name:
        return "[error: name is required]"

    try:
        safe_name(name, "skill")
    except ValueError as e:
        return f"[error: {e}]"

    skill_dir = _skills_dir() / name
    if not skill_dir.exists():
        return f"[error: skill '{name}' not found]"

    shutil.rmtree(skill_dir)
    return f"Deleted skill '{name}'"


@tool(description="List all available skills with their descriptions and status.")
async def list_skills() -> str:
    """List skills."""
    skills = scan_skills(_skills_dir())
    skill_filter = get_skill_filter()
    if skill_filter is not None:
        skills = [s for s in skills if skill_filter(s.name)]
    if not skills:
        return "No skills found."

    lines: list[str] = []
    for s in skills:
        env_note = ""
        if s.env_missing:
            env_note = f" (missing env: {', '.join(s.env_missing)})"
        elif s.env:
            env_note = " (env: ok)"
        lines.append(f"- **{s.name}**: {s.description}{env_note}")
    return "\n".join(lines)
