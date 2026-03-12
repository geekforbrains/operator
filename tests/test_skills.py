"""Tests for skill discovery, frontmatter parsing, and prompt assembly."""

from __future__ import annotations

from pathlib import Path

from operator_ai.frontmatter import extract_body, parse_frontmatter
from operator_ai.skills import (
    SkillInfo,
    build_skill_file,
    build_skills_prompt,
    scan_skills,
    validate_skill_name,
)


def _make_skill(
    skills_dir: Path,
    name: str,
    *,
    description: str = "A test skill",
    extra_fm: str = "",
    body: str = "",
) -> Path:
    """Create a skill directory with SKILL.md."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    body_text = body or f"# {name}\n\nInstructions for {name}."
    fm = f"name: {name}\ndescription: {description}"
    if extra_fm:
        fm += f"\n{extra_fm}"
    (skill_dir / "SKILL.md").write_text(f"---\n{fm}\n---\n\n{body_text}\n")
    return skill_dir


# ── scan_skills ──────────────────────────────────────────────────


def test_scan_skills_empty_dir(tmp_path: Path) -> None:
    """Empty directory returns no skills."""
    assert scan_skills(tmp_path) == []


def test_scan_skills_nonexistent_dir(tmp_path: Path) -> None:
    """Non-existent directory returns no skills."""
    assert scan_skills(tmp_path / "missing") == []


def test_scan_skills_finds_valid_skills(tmp_path: Path) -> None:
    """Valid skill directories are discovered."""
    _make_skill(tmp_path, "research", description="Do research")
    _make_skill(tmp_path, "code-review", description="Review code")

    skills = scan_skills(tmp_path)
    assert len(skills) == 2
    names = {s.name for s in skills}
    assert names == {"research", "code-review"}


def test_scan_skills_ignores_files(tmp_path: Path) -> None:
    """Non-directory entries are ignored."""
    (tmp_path / "README.md").write_text("Not a skill")
    _make_skill(tmp_path, "valid-skill")

    skills = scan_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "valid-skill"


def test_scan_skills_ignores_dirs_without_skill_md(tmp_path: Path) -> None:
    """Directories without SKILL.md are ignored."""
    (tmp_path / "no-skill-md").mkdir()
    _make_skill(tmp_path, "has-skill-md")

    skills = scan_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "has-skill-md"


def test_scan_skills_reports_env_vars(tmp_path: Path) -> None:
    """Skills with metadata.env report missing env vars."""
    _make_skill(
        tmp_path,
        "api-skill",
        extra_fm="metadata:\n  env:\n    - NONEXISTENT_VAR_12345",
    )

    skills = scan_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].env == ["NONEXISTENT_VAR_12345"]
    assert skills[0].env_missing == ["NONEXISTENT_VAR_12345"]


def test_scan_skills_location_is_skill_md_path(tmp_path: Path) -> None:
    """Skill location should point to the SKILL.md file."""
    _make_skill(tmp_path, "my-skill")

    skills = scan_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].location == str(tmp_path / "my-skill" / "SKILL.md")


# ── validate_skill_name ──────────────────────────────────────────


def test_valid_name() -> None:
    assert validate_skill_name("my-skill") is None


def test_empty_name() -> None:
    assert validate_skill_name("") is not None


def test_name_too_long() -> None:
    err = validate_skill_name("a" * 65)
    assert err is not None
    assert "64" in err


def test_name_consecutive_hyphens() -> None:
    err = validate_skill_name("my--skill")
    assert err is not None
    assert "consecutive" in err


def test_name_leading_hyphen() -> None:
    assert validate_skill_name("-skill") is not None


def test_name_uppercase() -> None:
    err = validate_skill_name("MySkill")
    assert err is not None
    assert "lowercase" in err


# ── build_skill_file ─────────────────────────────────────────────


def test_build_skill_file_minimal() -> None:
    content = build_skill_file(
        name="my-skill",
        description="A useful skill",
        instructions="# My Skill\n\nDo the thing.",
    )
    assert "---" in content
    assert "name: my-skill" in content
    assert "description: A useful skill" in content
    assert "# My Skill" in content
    assert "metadata" not in content


def test_build_skill_file_with_env() -> None:
    content = build_skill_file(
        name="api-skill",
        description="Calls APIs",
        instructions="# API Skill\n\nUse the API.",
        env=["API_KEY", "API_SECRET"],
    )
    assert "metadata" in content
    assert "API_KEY" in content
    assert "API_SECRET" in content


# ── build_skills_prompt ──────────────────────────────────────────


def test_build_skills_prompt_empty() -> None:
    """Empty skills list returns empty string."""
    assert build_skills_prompt([]) == ""


def test_build_skills_prompt_with_skills() -> None:
    """Skills are rendered as markdown."""
    skills = [
        SkillInfo(name="research", description="Do research", location="/path/to/SKILL.md"),
        SkillInfo(name="deploy", description="Deploy things", location="/path/to/deploy/SKILL.md"),
    ]
    prompt = build_skills_prompt(skills)
    assert "# Available Skills" in prompt
    assert "**research**" in prompt
    assert "**deploy**" in prompt
    assert "Do research" in prompt


def test_build_skills_prompt_shows_missing_env() -> None:
    """Skills with missing env vars show a warning."""
    skills = [
        SkillInfo(
            name="api-skill",
            description="API calls",
            location="/path",
            env=["API_KEY"],
            env_missing=["API_KEY"],
        ),
    ]
    prompt = build_skills_prompt(skills)
    assert "missing env" in prompt
    assert "API_KEY" in prompt


# ── Skill filter in prompt assembly ──────────────────────────────


def test_skill_filter_in_prompt_loading(tmp_path: Path) -> None:
    """Skills can be filtered before prompt assembly."""
    _make_skill(tmp_path, "allowed")
    _make_skill(tmp_path, "blocked")

    all_skills = scan_skills(tmp_path)
    assert len(all_skills) == 2

    filtered = [s for s in all_skills if s.name == "allowed"]
    assert len(filtered) == 1
    prompt = build_skills_prompt(filtered)
    assert "allowed" in prompt
    assert "blocked" not in prompt


# ── parse_frontmatter / extract_body ─────────────────────────────


def test_parse_frontmatter_valid() -> None:
    text = "---\nname: test\ndescription: A test\n---\n\n# Body"
    fm = parse_frontmatter(text)
    assert fm is not None
    assert fm["name"] == "test"


def test_parse_frontmatter_no_frontmatter() -> None:
    text = "# Just a heading\n\nSome content."
    assert parse_frontmatter(text) is None


def test_parse_frontmatter_empty_string() -> None:
    assert parse_frontmatter("") is None


def test_extract_body() -> None:
    text = "---\nname: test\n---\n\n# Body\n\nContent here."
    body = extract_body(text)
    assert body == "# Body\n\nContent here."


def test_extract_body_no_frontmatter() -> None:
    text = "# Just content\n\nParagraph."
    body = extract_body(text)
    assert body == "# Just content\n\nParagraph."


def test_parse_frontmatter_with_bom() -> None:
    """UTF-8 BOM at start should not prevent parsing."""
    text = "\ufeff---\nname: test\ndescription: BOM test\n---\n\nBody"
    fm = parse_frontmatter(text)
    assert fm is not None
    assert fm["name"] == "test"
