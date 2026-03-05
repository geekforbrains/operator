from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from operator_ai.cli import _STARTER_CONFIG, app

runner = CliRunner()


def test_starter_config_contains_roles():
    assert "roles:" in _STARTER_CONFIG
    assert "guest:" in _STARTER_CONFIG


def test_starter_config_contains_settings():
    assert "settings:" in _STARTER_CONFIG
    assert "reject_response: ignore" in _STARTER_CONFIG


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
