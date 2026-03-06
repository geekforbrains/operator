from __future__ import annotations

import shutil

from operator_ai.config import SKILLS_DIR
from operator_ai.skills import (
    extract_body,
    parse_frontmatter,
    scan_skills,
    validate_skill_frontmatter,
)
from operator_ai.tools.context import get_skill_filter
from operator_ai.tools.registry import safe_name, tool


@tool(
    description="Manage skills. Actions: list, create, update, delete.",
)
async def manage_skill(action: str, name: str = "", config: str = "") -> str:
    """Manage skills.

    Args:
        action: One of: list, create, update, delete.
        name: Skill directory name (required for create, update, delete).
        config: Full SKILL.md content for create/update. YAML frontmatter (between --- delimiters) with required fields: name, description. Optional: license, compatibility, metadata (with metadata.env for required env vars). Body is the skill instructions in markdown.
    """
    action = action.lower().strip()

    if action == "list":
        return _list_skills()
    elif action == "create":
        return _create_skill(name, config)
    elif action == "update":
        return _update_skill(name, config)
    elif action == "delete":
        return _delete_skill(name)
    else:
        return f"[error: unknown action '{action}'. Use: list, create, update, delete]"


def _list_skills() -> str:
    skills = scan_skills(SKILLS_DIR)
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
        lines.append(f"- **{s.name}**: {s.description}{env_note}\n  Location: `{s.location}`")
    return "\n".join(lines)


def _validate_and_parse(name: str, config: str) -> str | None:
    """Parse and validate config.

    Returns an optional warning string on success, or raises ``ValueError``
    if validation fails.
    """
    if not name:
        raise ValueError("'name' is required")
    if not config:
        raise ValueError("'config' (SKILL.md content) is required")

    fm = parse_frontmatter(config)
    if not fm:
        raise ValueError("config must have YAML frontmatter between --- delimiters")

    err = validate_skill_frontmatter(fm, safe_name(name, "skill"))
    if err:
        raise ValueError(err)

    body = extract_body(config)
    if not body.strip():
        raise ValueError(
            "skill body must not be empty — include instructions after the frontmatter"
        )

    line_count = len(body.strip().splitlines())
    if line_count > 500:
        return (
            f"\n[warning: body is {line_count} lines — recommended max is 500. "
            "Consider splitting into references/ files.]"
        )
    return None


def _create_skill(name: str, config: str) -> str:
    try:
        warning = _validate_and_parse(name, config)
    except ValueError as e:
        return f"[error: {e}]"

    skill_dir = SKILLS_DIR / name
    if skill_dir.exists():
        return f"[error: skill '{name}' already exists. Use 'update' to modify.]"

    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(config)
    return f"Created skill '{name}' at {skill_dir}{warning or ''}"


def _update_skill(name: str, config: str) -> str:
    try:
        warning = _validate_and_parse(name, config)
    except ValueError as e:
        return f"[error: {e}]"

    skill_dir = SKILLS_DIR / name
    if not skill_dir.exists():
        return f"[error: skill '{name}' not found]"

    (skill_dir / "SKILL.md").write_text(config)
    return f"Updated skill '{name}'{warning or ''}"


def _delete_skill(name: str) -> str:
    if not name:
        return "[error: 'name' is required for delete]"

    try:
        slug = safe_name(name, "skill")
    except ValueError as e:
        return f"[error: {e}]"

    skill_dir = SKILLS_DIR / slug
    if not skill_dir.exists():
        return f"[error: skill '{name}' not found]"

    shutil.rmtree(skill_dir)
    return f"Deleted skill '{name}'"
