from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

from operator_ai.tools.context import set_skill_filter
from operator_ai.tools.skills_access import (
    _check_skill_access,
    read_skill,
    run_skill,
)
from operator_ai.tools.workspace import set_workspace


def _make_skill(tmp_path: Path, name: str, skill_md: str = "") -> Path:
    """Create a minimal skill directory with SKILL.md."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = skill_md or textwrap.dedent(f"""\
        ---
        name: {name}
        description: Test skill
        ---

        # {name}

        This is a test skill.
    """)
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


# --- _check_skill_access tests ---


def test_check_skill_access_invalid_names() -> None:
    """Invalid skill names (empty, slashes, ..) should return error."""
    assert _check_skill_access("") is not None
    assert _check_skill_access("foo/bar") is not None
    assert _check_skill_access("foo\\bar") is not None
    assert _check_skill_access("..") is not None
    assert _check_skill_access("foo/../bar") is not None


def test_check_skill_access_filter_blocks(tmp_path: Path) -> None:
    """Skill not in the filter should be blocked."""
    _make_skill(tmp_path, "blocked-skill")
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(lambda name: name == "allowed-skill")
        try:
            result = _check_skill_access("blocked-skill")
            assert result is not None
            assert "not available" in result
        finally:
            set_skill_filter(None)


def test_check_skill_access_filter_allows(tmp_path: Path) -> None:
    """Skill in the filter should be allowed."""
    _make_skill(tmp_path, "allowed-skill")
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(lambda name: name == "allowed-skill")
        try:
            result = _check_skill_access("allowed-skill")
            assert result is None
        finally:
            set_skill_filter(None)


def test_check_skill_access_no_filter(tmp_path: Path) -> None:
    """No filter means all skills accessible (if dir exists)."""
    _make_skill(tmp_path, "my-skill")
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = _check_skill_access("my-skill")
        assert result is None


def test_check_skill_access_dir_not_found(tmp_path: Path) -> None:
    """Skill directory doesn't exist should return error."""
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = _check_skill_access("nonexistent")
        assert result is not None
        assert "not found" in result


# --- read_skill tests ---


def test_read_skill_reads_skill_md(tmp_path: Path) -> None:
    """Reading with empty path should return SKILL.md content."""
    _make_skill(tmp_path, "my-skill")
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = asyncio.run(read_skill("my-skill"))
        assert "my-skill" in result
        assert "Test skill" in result


def test_read_skill_reads_specific_path(tmp_path: Path) -> None:
    """Reading with a specific path should return that file."""
    skill_dir = _make_skill(tmp_path, "my-skill")
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "api.md").write_text("# API Reference\nSome content.")
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = asyncio.run(read_skill("my-skill", "references/api.md"))
        assert "API Reference" in result


def test_read_skill_blocks_path_traversal(tmp_path: Path) -> None:
    """Path traversal with .. should be blocked."""
    _make_skill(tmp_path, "my-skill")
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = asyncio.run(read_skill("my-skill", "../../../etc/passwd"))
        assert "error" in result.lower()
        assert "traversal" in result.lower()


def test_read_skill_filter_blocks(tmp_path: Path) -> None:
    """Skill not in the filter should be blocked."""
    _make_skill(tmp_path, "secret-skill")
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(lambda name: name == "other-skill")
        try:
            result = asyncio.run(read_skill("secret-skill"))
            assert "error" in result.lower()
            assert "not available" in result
        finally:
            set_skill_filter(None)


# --- run_skill tests ---


def test_run_skill_simple_command(tmp_path: Path) -> None:
    """Run a simple command in skill context."""
    _make_skill(tmp_path, "my-skill")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = asyncio.run(run_skill("my-skill", f"{sys.executable} -c \"print('hello')\""))
        assert "hello" in result


def test_run_skill_path_expansion(tmp_path: Path) -> None:
    """Args starting with scripts/ should be expanded to full path."""
    skill_dir = _make_skill(tmp_path, "my-skill")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    script_file = scripts / "test.py"
    script_file.write_text("import sys; print(sys.argv[1])")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = asyncio.run(run_skill("my-skill", f"{sys.executable} scripts/test.py"))
        # The script prints its first argument, which should be the expanded path
        expected_path = str(skill_dir / "scripts" / "test.py")
        assert expected_path in result


def test_run_skill_sets_skill_dir_env(tmp_path: Path) -> None:
    """SKILL_DIR env var should be set to the skill directory."""
    skill_dir = _make_skill(tmp_path, "my-skill")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(None)
        result = asyncio.run(
            run_skill(
                "my-skill",
                f"{sys.executable} -c \"import os; print(os.environ['SKILL_DIR'])\"",
            )
        )
        assert str(skill_dir) in result


def test_run_skill_expands_env_var_refs(tmp_path: Path) -> None:
    """$VAR and ${VAR} args should expand against the sanitized env."""
    _make_skill(tmp_path, "my-skill")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    os.environ["SKILL_TEST_TOKEN"] = "secret123"
    try:
        with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
            set_skill_filter(None)
            result = asyncio.run(
                run_skill(
                    "my-skill",
                    f'{sys.executable} -c "import sys; print(sys.argv[1]); print(sys.argv[2])" '
                    "'$SKILL_TEST_TOKEN' '${SKILL_TEST_TOKEN}'",
                )
            )
            assert result.splitlines() == ["secret123", "secret123"]
    finally:
        os.environ.pop("SKILL_TEST_TOKEN", None)


def test_run_skill_preserves_operator_home_env(tmp_path: Path) -> None:
    """OPERATOR_HOME should remain available to skill commands."""
    _make_skill(tmp_path, "my-skill")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    os.environ["OPERATOR_HOME"] = "/tmp/operator-home"
    try:
        with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
            set_skill_filter(None)
            result = asyncio.run(
                run_skill(
                    "my-skill",
                    f"{sys.executable} -c \"import os, sys; print(sys.argv[1]); print(os.environ['OPERATOR_HOME'])\" "
                    "'$OPERATOR_HOME'",
                )
            )
            assert result.splitlines() == ["/tmp/operator-home", "/tmp/operator-home"]
    finally:
        os.environ.pop("OPERATOR_HOME", None)


def test_run_skill_filter_blocks(tmp_path: Path) -> None:
    """Skill not in the filter should be blocked."""
    _make_skill(tmp_path, "secret-skill")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
        set_skill_filter(lambda name: name == "other-skill")
        try:
            result = asyncio.run(run_skill("secret-skill", "echo hi"))
            assert "error" in result.lower()
            assert "not available" in result
        finally:
            set_skill_filter(None)


def test_run_skill_strips_operator_env_vars(tmp_path: Path) -> None:
    """OPERATOR_* env vars should be stripped."""
    _make_skill(tmp_path, "my-skill")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    os.environ["OPERATOR_TEST_SECRET"] = "should_be_stripped"
    try:
        with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
            set_skill_filter(None)
            result = asyncio.run(
                run_skill(
                    "my-skill",
                    f"{sys.executable} -c \"import os; print(os.environ.get('OPERATOR_TEST_SECRET', 'not_found'))\"",
                )
            )
            assert "not_found" in result
    finally:
        os.environ.pop("OPERATOR_TEST_SECRET", None)


def test_run_skill_does_not_expand_stripped_operator_env_refs(tmp_path: Path) -> None:
    """Stripped OPERATOR_* vars should not leak through argv expansion."""
    _make_skill(tmp_path, "my-skill")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_workspace(workspace)
    os.environ["OPERATOR_TEST_SECRET"] = "should_be_stripped"
    try:
        with patch("operator_ai.tools.skills_access._skills_dir", return_value=tmp_path):
            set_skill_filter(None)
            result = asyncio.run(
                run_skill(
                    "my-skill",
                    f"{sys.executable} -c \"import sys; print(sys.argv[1])\" '$OPERATOR_TEST_SECRET'",
                )
            )
            assert result.splitlines() == ["$OPERATOR_TEST_SECRET"]
    finally:
        os.environ.pop("OPERATOR_TEST_SECRET", None)
