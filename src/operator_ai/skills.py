"""Skill discovery and prompt injection."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from operator_ai.frontmatter import parse_frontmatter

logger = logging.getLogger("operator.skills")

# agentskills.io name rules: 1-64 chars, lowercase alphanumeric + hyphens,
# no leading/trailing/consecutive hyphens.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


@dataclass
class SkillInfo:
    name: str
    description: str
    location: str
    env: list[str] = field(default_factory=list)
    env_missing: list[str] = field(default_factory=list)


def scan_skills(skills_dir: Path) -> list[SkillInfo]:
    """Scan skills directory, parse SKILL.md frontmatter, return skill metadata."""
    skills: list[SkillInfo] = []
    if not skills_dir.is_dir():
        return skills

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            frontmatter = parse_frontmatter(skill_md.read_text())
            if frontmatter:
                metadata = frontmatter.get("metadata") or {}
                env_vars = metadata.get("env") or []
                if isinstance(env_vars, str):
                    env_vars = [env_vars]
                missing = [v for v in env_vars if not os.environ.get(v)]
                if missing:
                    logger.warning(
                        "Skill '%s' missing env vars: %s",
                        frontmatter.get("name", skill_dir.name),
                        ", ".join(missing),
                    )

                skills.append(
                    SkillInfo(
                        name=frontmatter.get("name", skill_dir.name),
                        description=frontmatter.get("description", ""),
                        location=str(skill_md),
                        env=env_vars,
                        env_missing=missing,
                    )
                )
        except Exception as e:
            logger.warning("Failed to parse %s: %s", skill_md, e)
    return skills


def build_skills_prompt(skills: list[SkillInfo]) -> str:
    """Build markdown block for system prompt injection."""
    if not skills:
        return ""
    lines = ["# Available Skills"]
    for s in skills:
        status = f" (missing env: {', '.join(s.env_missing)})" if s.env_missing else ""
        lines.append(f"\n- **{s.name}**: {s.description}{status}")
    return "\n".join(lines)


def build_skill_file(
    *,
    name: str,
    description: str,
    instructions: str,
    env: list[str] | None = None,
) -> str:
    """Assemble a SKILL.md file from structured fields."""
    fm: dict = {"name": name, "description": description}
    if env:
        fm["metadata"] = {"env": env}
    fm_text = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{fm_text}\n---\n\n{instructions}\n"


def validate_skill_name(name: str) -> str | None:
    """Validate a skill name per agentskills.io spec. Returns error string or None."""
    if not name:
        return "name is required"
    if len(name) > 64:
        return f"name must be <= 64 characters, got {len(name)}"
    if "--" in name:
        return "name must not contain consecutive hyphens"
    if not _NAME_RE.match(name):
        return "name must be lowercase alphanumeric + hyphens, no leading/trailing hyphens"
    return None
