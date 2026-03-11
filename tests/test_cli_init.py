from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from operator_ai.cli import _STARTER_CONFIG, _generate_plist, _generate_systemd_unit, app
from operator_ai.config import load_config
from operator_ai.store import Store

runner = CliRunner()


def test_starter_config_contains_roles():
    assert "roles:" in _STARTER_CONFIG
    assert "guest:" in _STARTER_CONFIG


def test_starter_config_contains_runtime() -> None:
    assert "runtime:" in _STARTER_CONFIG
    assert "show_usage: false" in _STARTER_CONFIG
    assert "reject_response: ignore" in _STARTER_CONFIG
    assert "env:" in _STARTER_CONFIG
    assert "settings:" in _STARTER_CONFIG


def test_starter_config_references_env_file():
    assert 'env_file: ".env"' in _STARTER_CONFIG


def test_init_creates_config_and_shows_setup_reminder(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()

    with (
        patch("operator_ai.cli._detect_local_timezone", return_value="America/Vancouver"),
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "operator setup" in result.output
    assert "operator user add" in result.output

    # Config file was written with expected content
    config_file = op_dir / "operator.yaml"
    assert config_file.exists()
    content = config_file.read_text()
    assert "roles:" in content
    assert "runtime:" in content
    assert "show_usage: false" in content
    assert "reject_response: ignore" in content
    assert "env:" in content
    assert "settings:" in content
    assert "permission_groups:" in content

    config = load_config(config_file)
    transport = config.agents["operator"].transport
    assert transport is not None
    assert transport.type == "slack"
    assert transport.env["bot_token"] == "SLACK_BOT_TOKEN"
    assert transport.env["app_token"] == "SLACK_APP_TOKEN"


def test_init_creates_env_file_with_api_key_placeholders(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()

    with (
        patch("operator_ai.cli._detect_local_timezone", return_value="America/Vancouver"),
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
    assert "# ANTHROPIC_API_KEY=sk-ant-..." in content
    assert "# OPENAI_API_KEY=sk-..." in content
    assert "# GEMINI_API_KEY=..." in content
    assert "# GOOGLE_API_KEY=..." in content
    assert "# LITELLM_LOG=DEBUG" in content
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


def test_service_start_bootstraps_launchd_agent_when_unloaded(tmp_path: Path):
    plist_path = tmp_path / "ai.operator.plist"
    plist_path.write_text("<plist />")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch("operator_ai.cli._is_macos", return_value=True),
        patch("operator_ai.cli._PLIST_PATH", plist_path),
        patch("operator_ai.cli._launchd_service_loaded", return_value=False),
        patch("operator_ai.cli._launchd_domain_target", return_value="gui/501"),
        patch("operator_ai.cli.subprocess.run", side_effect=fake_run),
    ):
        result = runner.invoke(app, ["service", "start"])

    assert result.exit_code == 0
    assert calls == [
        (
            ["launchctl", "bootstrap", "gui/501", str(plist_path)],
            {"check": True},
        )
    ]


def test_service_stop_boots_out_launchd_agent_on_macos():
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch("operator_ai.cli._is_macos", return_value=True),
        patch("operator_ai.cli._launchd_service_loaded", return_value=True),
        patch("operator_ai.cli._launchd_service_target", return_value="gui/501/ai.operator"),
        patch("operator_ai.cli.subprocess.run", side_effect=fake_run),
    ):
        result = runner.invoke(app, ["service", "stop"])

    assert result.exit_code == 0
    assert calls == [
        (
            ["launchctl", "bootout", "gui/501/ai.operator"],
            {"check": True},
        )
    ]


def test_service_restart_kickstarts_loaded_launchd_agent_on_macos(tmp_path: Path):
    plist_path = tmp_path / "ai.operator.plist"
    plist_path.write_text("<plist />")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch("operator_ai.cli._is_macos", return_value=True),
        patch("operator_ai.cli._PLIST_PATH", plist_path),
        patch("operator_ai.cli._launchd_service_loaded", return_value=True),
        patch("operator_ai.cli._launchd_service_target", return_value="gui/501/ai.operator"),
        patch("operator_ai.cli.subprocess.run", side_effect=fake_run),
    ):
        result = runner.invoke(app, ["service", "restart"])

    assert result.exit_code == 0
    assert calls == [
        (
            ["launchctl", "kickstart", "-k", "gui/501/ai.operator"],
            {"check": True},
        )
    ]


def test_init_skips_existing_env_file(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()
    env_file = op_dir / ".env"
    env_file.write_text("EXISTING=yes\n")

    with (
        patch("operator_ai.cli._detect_local_timezone", return_value="America/Vancouver"),
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    # Should not have overwritten the existing file
    assert env_file.read_text() == "EXISTING=yes\n"


def test_setup_creates_env_and_admin_user(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    store = Store(path=tmp_path / "setup.db")

    with (
        patch("operator_ai.cli._detect_local_timezone", return_value="America/Vancouver"),
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.cli.get_store", return_value=store),
        patch("operator_ai.cli.getpass.getuser", return_value="gavin"),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
    ):
        result = runner.invoke(
            app,
            ["setup", "--no-run"],
            input="\n\n\nsk-ant-key\nxoxb-bot-token\nxapp-app-token\nU123ABC45\n",
        )

    assert result.exit_code == 0
    assert "Ready" in result.output
    assert "Send a first message like" in result.output

    config_file = op_dir / "operator.yaml"
    env_file = op_dir / ".env"
    assert config_file.exists()
    assert env_file.exists()

    config = load_config(config_file)
    transport = config.agents["operator"].transport
    assert transport is not None
    assert transport.env["bot_token"] == "SLACK_BOT_TOKEN"
    assert transport.env["app_token"] == "SLACK_APP_TOKEN"

    env_content = env_file.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-key" in env_content
    assert "SLACK_BOT_TOKEN=xoxb-bot-token" in env_content
    assert "SLACK_APP_TOKEN=xapp-app-token" in env_content
    user = store.get_user("gavin")
    assert user is not None
    assert "admin" in user.roles
    assert "slack:U123ABC45" in user.identities


def test_setup_run_invokes_runtime(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    store = Store(path=tmp_path / "setup.db")
    async_main = AsyncMock()

    with (
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.cli.get_store", return_value=store),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
        patch("operator_ai.cli.async_main", async_main),
    ):
        result = runner.invoke(
            app,
            [
                "setup",
                "--provider",
                "openai",
                "--timezone",
                "Europe/London",
                "--username",
                "gavin",
                "--api-key",
                "sk-openai-key",
                "--secret",
                "SLACK_BOT_TOKEN=xoxb-bot-token",
                "--secret",
                "SLACK_APP_TOKEN=xapp-app-token",
                "--identity",
                "U123ABC45",
                "--run",
            ],
        )

    assert result.exit_code == 0
    async_main.assert_awaited_once()

    env_content = (op_dir / ".env").read_text()
    assert "OPENAI_API_KEY=sk-openai-key" in env_content

    config_content = (op_dir / "operator.yaml").read_text()
    assert '    - "openai/gpt-4.1"' in config_content


def test_setup_gemini_uses_google_api_key_from_env_file(tmp_path: Path):
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()
    (op_dir / ".env").write_text("GOOGLE_API_KEY=google-key\n")
    store = Store(path=tmp_path / "setup.db")

    with (
        patch("operator_ai.cli.OPERATOR_DIR", op_dir),
        patch("operator_ai.cli.get_store", return_value=store),
        patch("operator_ai.skills.install_bundled_skills", return_value=[]),
    ):
        result = runner.invoke(
            app,
            [
                "setup",
                "--provider",
                "gemini",
                "--timezone",
                "America/Toronto",
                "--username",
                "gavin",
                "--identity",
                "U123ABC45",
                "--secret",
                "SLACK_BOT_TOKEN=xoxb-bot-token",
                "--secret",
                "SLACK_APP_TOKEN=xapp-app-token",
                "--no-run",
            ],
        )

    assert result.exit_code == 0
    env_content = (op_dir / ".env").read_text()
    assert "GOOGLE_API_KEY=google-key" in env_content
    assert "GEMINI_API_KEY=" not in env_content
    assert "permission_groups:" in (op_dir / "operator.yaml").read_text()
