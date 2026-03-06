from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from operator_ai.cli import _STARTER_CONFIG, _generate_plist, _generate_systemd_unit, app

runner = CliRunner()


def test_starter_config_contains_roles():
    assert "roles:" in _STARTER_CONFIG
    assert "guest:" in _STARTER_CONFIG


def test_starter_config_contains_settings():
    assert "settings:" in _STARTER_CONFIG
    assert "reject_response: ignore" in _STARTER_CONFIG


def test_starter_config_references_env_file():
    assert 'env_file: ".env"' in _STARTER_CONFIG


def test_init_creates_config_and_shows_user_add_reminder(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()

    with (
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "operator user add" in result.output

    # Config file was written with expected content
    config_file = op_dir / "operator.yaml"
    assert config_file.exists()
    content = config_file.read_text()
    assert "roles:" in content
    assert "settings:" in content
    assert "reject_response: ignore" in content


def test_init_creates_env_file_with_api_key_placeholders(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()

    with (
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0

    env_file = op_dir / ".env"
    assert env_file.exists()
    content = env_file.read_text()
    # Should NOT contain PATH (PATH is embedded in the service definition instead)
    assert "PATH=" not in content
    # Should contain API key placeholder comments
    assert "# ANTHROPIC_API_KEY=sk-..." in content
    assert "# OPENAI_API_KEY=sk-..." in content
    # Should have restricted permissions
    mode = env_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_generate_plist_embeds_path():
    plist = _generate_plist("/usr/local/bin/operator")
    assert "<key>EnvironmentVariables</key>" in plist
    assert "<key>PATH</key>" in plist
    # The PATH value should be present (at minimum /usr/bin:/bin)
    assert "/usr" in plist or "/bin" in plist


def test_generate_systemd_unit_embeds_path():
    unit = _generate_systemd_unit("/usr/local/bin/operator")
    assert "Environment=PATH=" in unit
    assert "/usr" in unit or "/bin" in unit


def test_init_skips_existing_env_file(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()
    env_file = op_dir / ".env"
    env_file.write_text("EXISTING=yes\n")

    with (
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    # Should not have overwritten the existing file
    assert env_file.read_text() == "EXISTING=yes\n"
