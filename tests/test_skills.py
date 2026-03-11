"""Tests for skill discovery, frontmatter validation, and prompt assembly."""

from __future__ import annotations

from pathlib import Path

from operator_ai.skills import (
    SkillInfo,
    build_skills_prompt,
    extract_body,
    install_bundled_skills,
    parse_frontmatter,
    scan_skills,
    validate_skill_frontmatter,
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


# ── validate_skill_frontmatter ──────────────────────────────────


def test_valid_frontmatter() -> None:
    """Valid frontmatter passes validation."""
    fm = {"name": "my-skill", "description": "A useful skill"}
    assert validate_skill_frontmatter(fm, "my-skill") is None


def test_missing_name() -> None:
    """Missing name field is an error."""
    fm = {"description": "No name"}
    err = validate_skill_frontmatter(fm, "my-skill")
    assert err is not None
    assert "name" in err


def test_missing_description() -> None:
    """Missing description field is an error."""
    fm = {"name": "my-skill"}
    err = validate_skill_frontmatter(fm, "my-skill")
    assert err is not None
    assert "description" in err


def test_name_mismatch() -> None:
    """Name not matching directory is an error."""
    fm = {"name": "wrong-name", "description": "A skill"}
    err = validate_skill_frontmatter(fm, "my-skill")
    assert err is not None
    assert "must match" in err


def test_name_too_long() -> None:
    """Name > 64 chars is an error."""
    long_name = "a" * 65
    fm = {"name": long_name, "description": "A skill"}
    err = validate_skill_frontmatter(fm, long_name)
    assert err is not None
    assert "64" in err


def test_name_consecutive_hyphens() -> None:
    """Consecutive hyphens in name is an error."""
    fm = {"name": "my--skill", "description": "A skill"}
    err = validate_skill_frontmatter(fm, "my--skill")
    assert err is not None
    assert "consecutive" in err


def test_name_leading_hyphen() -> None:
    """Leading hyphen in name is an error."""
    fm = {"name": "-skill", "description": "A skill"}
    err = validate_skill_frontmatter(fm, "-skill")
    assert err is not None


def test_name_uppercase() -> None:
    """Uppercase in name is an error."""
    fm = {"name": "MySkill", "description": "A skill"}
    err = validate_skill_frontmatter(fm, "MySkill")
    assert err is not None
    assert "lowercase" in err


def test_description_too_long() -> None:
    """Description > 1024 chars is an error."""
    fm = {"name": "my-skill", "description": "x" * 1025}
    err = validate_skill_frontmatter(fm, "my-skill")
    assert err is not None
    assert "1024" in err


def test_metadata_env_as_string() -> None:
    """metadata.env as a single string is accepted."""
    fm = {
        "name": "my-skill",
        "description": "A skill",
        "metadata": {"env": "MY_VAR"},
    }
    assert validate_skill_frontmatter(fm, "my-skill") is None


def test_metadata_env_as_list() -> None:
    """metadata.env as a list of strings is accepted."""
    fm = {
        "name": "my-skill",
        "description": "A skill",
        "metadata": {"env": ["VAR1", "VAR2"]},
    }
    assert validate_skill_frontmatter(fm, "my-skill") is None


def test_metadata_env_invalid_type() -> None:
    """metadata.env as non-string non-list is an error."""
    fm = {
        "name": "my-skill",
        "description": "A skill",
        "metadata": {"env": 42},
    }
    err = validate_skill_frontmatter(fm, "my-skill")
    assert err is not None
    assert "env" in err


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


# ── install_bundled_skills ───────────────────────────────────────


def test_install_bundled_skills_copies(tmp_path: Path) -> None:
    """Bundled skills are copied to the target directory."""
    # Create a fake bundled skill source
    source = tmp_path / "bundled"
    source.mkdir()
    skill_src = source / "demo-skill"
    skill_src.mkdir()
    (skill_src / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo\n---\n\n# Demo\n")

    target = tmp_path / "skills"

    # Patch BUNDLED_SKILLS_DIR for this test
    import operator_ai.skills as skills_mod

    orig = skills_mod.BUNDLED_SKILLS_DIR
    try:
        skills_mod.BUNDLED_SKILLS_DIR = source
        installed = install_bundled_skills(target)
    finally:
        skills_mod.BUNDLED_SKILLS_DIR = orig

    assert "demo-skill" in installed
    assert (target / "demo-skill" / "SKILL.md").exists()


def test_install_bundled_skills_skips_existing(tmp_path: Path) -> None:
    """Bundled skills are not overwritten if already present."""
    source = tmp_path / "bundled"
    source.mkdir()
    skill_src = source / "demo-skill"
    skill_src.mkdir()
    (skill_src / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo\n---\n\n# Demo\n")

    target = tmp_path / "skills"
    (target / "demo-skill").mkdir(parents=True)
    (target / "demo-skill" / "SKILL.md").write_text("custom content")

    import operator_ai.skills as skills_mod

    orig = skills_mod.BUNDLED_SKILLS_DIR
    try:
        skills_mod.BUNDLED_SKILLS_DIR = source
        installed = install_bundled_skills(target)
    finally:
        skills_mod.BUNDLED_SKILLS_DIR = orig

    assert installed == []
    # Content should not be overwritten
    assert (target / "demo-skill" / "SKILL.md").read_text() == "custom content"


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
