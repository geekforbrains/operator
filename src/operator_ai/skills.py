from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("operator.skills")

BUNDLED_SKILLS_DIR = Path(__file__).parent / "bundled_skills"

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


def validate_skill_frontmatter(fm: dict[str, Any], dir_name: str) -> str | None:
    """Validate skill frontmatter per agentskills.io spec. Returns error string or None."""
    name = fm.get("name")
    if not name or not isinstance(name, str):
        return "frontmatter must include a 'name' field"
    if len(name) > 64:
        return f"name must be <= 64 characters, got {len(name)}"
    if "--" in name:
        return "name must not contain consecutive hyphens"
    if not _NAME_RE.match(name):
        return "name must be lowercase alphanumeric + hyphens, no leading/trailing hyphens"
    if name != dir_name:
        return f"name '{name}' must match directory name '{dir_name}'"

    desc = fm.get("description")
    if not desc or not isinstance(desc, str):
        return "frontmatter must include a 'description' field"
    if len(desc) > 1024:
        return f"description must be <= 1024 characters, got {len(desc)}"

    metadata = fm.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            return "metadata must be a mapping"
        env_vars = metadata.get("env")
        if env_vars is not None:
            if isinstance(env_vars, str):
                env_vars = [env_vars]
            if not isinstance(env_vars, list) or not all(isinstance(v, str) for v in env_vars):
                return "metadata.env must be a list of strings"

    return None


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


def install_bundled_skills(target_dir: Path) -> list[str]:
    """Copy bundled skills into target_dir. Only copies skills whose directory doesn't exist yet.

    Returns list of skill names that were installed.
    """
    installed: list[str] = []
    if not BUNDLED_SKILLS_DIR.is_dir():
        return installed

    target_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(BUNDLED_SKILLS_DIR.iterdir()):
        if not src.is_dir() or not (src / "SKILL.md").exists():
            continue
        dest = target_dir / src.name
        if dest.exists():
            continue
        shutil.copytree(src, dest)
        installed.append(src.name)
        logger.info("Installed bundled skill '%s'", src.name)
    return installed


def reset_bundled_skill(name: str, target_dir: Path) -> bool:
    """Reset a bundled skill to its original version. Returns True on success."""
    src = BUNDLED_SKILLS_DIR / name
    if not src.is_dir() or not (src / "SKILL.md").exists():
        return False
    dest = target_dir / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    logger.info("Reset bundled skill '%s'", name)
    return True


def list_bundled_skill_names() -> list[str]:
    """Return names of all bundled skills."""
    if not BUNDLED_SKILLS_DIR.is_dir():
        return []
    return sorted(
        d.name for d in BUNDLED_SKILLS_DIR.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
    )


def build_skills_prompt(skills: list[SkillInfo]) -> str:
    """Build markdown block for system prompt injection."""
    if not skills:
        return ""
    lines = ["# Available Skills"]
    for s in skills:
        status = f" (missing env: {', '.join(s.env_missing)})" if s.env_missing else ""
        lines.append(f"\n- **{s.name}**: {s.description}{status}")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    """Parse YAML frontmatter between --- delimiters."""
    split = _split_frontmatter(text)
    if split is None:
        return None
    frontmatter_text, _ = split
    try:
        parsed = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def extract_body(text: str) -> str:
    """Return the markdown body after the --- frontmatter block."""
    split = _split_frontmatter(text)
    if split is None:
        return text.strip()
    _, body = split
    return body.strip()


def rewrite_frontmatter(path: Path, updates: dict) -> bool:
    """Update specific fields in a file's YAML frontmatter, preserving the body.

    Returns True on success, False if frontmatter couldn't be parsed.
    """
    text = path.read_text()
    fm = parse_frontmatter(text)
    if not fm:
        return False
    fm.update(updates)
    body = extract_body(text)
    new_fm = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    path.write_text(f"---\n{new_fm}\n---\n\n{body}\n")
    return True


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split markdown into (frontmatter, body) when fenced by top-level --- lines."""
    if not text:
        return None

    # Allow UTF-8 BOM at file start.
    normalized = text.lstrip("\ufeff")
    lines = normalized.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            frontmatter = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :])
            return frontmatter, body
    return None
