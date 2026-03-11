from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from operator_ai.cli import app
from operator_ai.store import Store

runner = CliRunner()


@pytest.fixture(autouse=True)
def _patch_store(tmp_path: Path):
    """Monkeypatch get_store() to return a Store backed by a temp database."""
    store = Store(path=tmp_path / "test.db")
    with patch("operator_ai.cli.get_store", return_value=store):
        yield store


def test_user_add():
    result = runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    assert result.exit_code == 0
    assert "gavin" in result.output
    assert "admin" in result.output
    assert "slack:U123" in result.output


def test_user_list():
    runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    result = runner.invoke(app, ["user", "list"])
    assert result.exit_code == 0
    assert "gavin" in result.output
    assert "admin" in result.output
    assert "slack:U123" in result.output


def test_user_info():
    runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    result = runner.invoke(app, ["user", "info", "gavin"])
    assert result.exit_code == 0
    assert "gavin" in result.output
    assert "admin" in result.output
    assert "slack:U123" in result.output


def test_user_link():
    runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    result = runner.invoke(app, ["user", "link", "gavin", "telegram", "456"])
    assert result.exit_code == 0
    assert "telegram:456" in result.output

    # Verify via info
    info = runner.invoke(app, ["user", "info", "gavin"])
    assert "telegram:456" in info.output


def test_user_unlink():
    runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    runner.invoke(app, ["user", "link", "gavin", "telegram", "456"])
    result = runner.invoke(app, ["user", "unlink", "gavin", "telegram", "456"])
    assert result.exit_code == 0
    assert "Unlinked" in result.output


def test_user_add_role():
    runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    result = runner.invoke(app, ["user", "add-role", "gavin", "team"])
    assert result.exit_code == 0
    assert "team" in result.output


def test_user_remove_role():
    runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    runner.invoke(app, ["user", "add-role", "gavin", "team"])
    result = runner.invoke(app, ["user", "remove-role", "gavin", "team"])
    assert result.exit_code == 0
    assert "Removed" in result.output


def test_user_remove():
    runner.invoke(app, ["user", "add", "gavin", "--role", "admin", "slack", "U123"])
    result = runner.invoke(app, ["user", "remove", "gavin"])
    assert result.exit_code == 0
    assert "removed" in result.output

    # Verify user is gone
    info = runner.invoke(app, ["user", "info", "gavin"])
    assert info.exit_code == 1


def test_user_add_invalid_name():
    result = runner.invoke(app, ["user", "add", "invalid_NAME", "--role", "admin", "slack", "U123"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_user_list_empty():
    result = runner.invoke(app, ["user", "list"])
    assert result.exit_code == 0
    assert "No users found" in result.output


def test_user_info_not_found():
    result = runner.invoke(app, ["user", "info", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_user_remove_not_found():
    result = runner.invoke(app, ["user", "remove", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_user_link_not_found():
    result = runner.invoke(app, ["user", "link", "ghost", "slack", "U999"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_user_unlink_not_found():
    result = runner.invoke(app, ["user", "unlink", "gavin", "slack", "U999"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_user_add_role_user_not_found():
    result = runner.invoke(app, ["user", "add-role", "ghost", "admin"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_user_remove_role_not_found():
    result = runner.invoke(app, ["user", "remove-role", "ghost", "admin"])
    assert result.exit_code == 1
    assert "does not have" in result.output


def test_tools_command():
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    assert "run_shell" in result.output
    assert "Transports may provide additional tools at runtime" in result.output
