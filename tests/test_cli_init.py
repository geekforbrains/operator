from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from operator_ai.cli import _STARTER_CONFIG, _generate_plist, _generate_systemd_unit, app
from operator_ai.config import load_config

runner = CliRunner()


def test_starter_config_contains_roles() -> None:
    assert "roles:" in _STARTER_CONFIG
    assert "guest:" in _STARTER_CONFIG


def test_starter_config_contains_runtime() -> None:
    assert "runtime:" in _STARTER_CONFIG
    assert "show_usage: false" in _STARTER_CONFIG
    assert "reject_response: ignore" in _STARTER_CONFIG
    assert 'thinking: "off"' in _STARTER_CONFIG
    assert "max_iterations: 25" in _STARTER_CONFIG
    assert "hook_timeout: 30" in _STARTER_CONFIG
    assert "env:" in _STARTER_CONFIG
    assert "settings:" in _STARTER_CONFIG


def test_starter_config_references_env_file() -> None:
    assert 'env_file: ".env"' in _STARTER_CONFIG


def test_init_creates_full_scaffold_and_points_user_to_manual_edits(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"

    with patch("operator_ai.cli.OPERATOR_DIR", op_dir):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    normalized_output = " ".join(result.output.split())
    assert "Edit" in normalized_output
    assert "operator.yaml" in normalized_output
    assert (
        "operator user add <username> --role admin slack <YOUR_SLACK_USER_ID>" in normalized_output
    )

    config_file = op_dir / "operator.yaml"
    env_file = op_dir / ".env"
    system_md = op_dir / "SYSTEM.md"
    agent_md = op_dir / "agents" / "operator" / "AGENT.md"

    assert config_file.exists()
    assert env_file.exists()
    assert system_md.exists()
    assert agent_md.exists()

    for path in (
        op_dir / "logs",
        op_dir / "jobs",
        op_dir / "skills",
        op_dir / "shared",
        op_dir / "db",
        op_dir / "memory" / "global" / "rules",
        op_dir / "memory" / "global" / "notes",
        op_dir / "memory" / "global" / "trash",
        op_dir / "memory" / "users",
        op_dir / "agents" / "operator" / "workspace" / "inbox",
        op_dir / "agents" / "operator" / "workspace" / "work",
        op_dir / "agents" / "operator" / "workspace" / "artifacts",
        op_dir / "agents" / "operator" / "workspace" / "tmp",
        op_dir / "agents" / "operator" / "memory" / "rules",
        op_dir / "agents" / "operator" / "memory" / "notes",
        op_dir / "agents" / "operator" / "memory" / "trash",
        op_dir / "agents" / "operator" / "state",
        op_dir / "shared" / "operator",
    ):
        assert path.is_dir()

    shared_link = op_dir / "agents" / "operator" / "workspace" / "shared"
    assert shared_link.is_symlink()
    assert shared_link.resolve() == (op_dir / "shared").resolve()

    content = config_file.read_text()
    assert "roles:" in content
    assert "runtime:" in content
    assert 'thinking: "off"' in content
    assert "max_iterations: 25" in content
    assert "hook_timeout: 30" in content
    assert "permission_groups:" in content

    config = load_config(config_file)
    transport = config.agents["operator"].transport
    assert transport is not None
    assert transport.type == "slack"
    assert transport.env["bot_token"] == "SLACK_BOT_TOKEN"
    assert transport.env["app_token"] == "SLACK_APP_TOKEN"


def test_init_creates_env_file_with_api_key_placeholders(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"

    with patch("operator_ai.cli.OPERATOR_DIR", op_dir):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0

    env_file = op_dir / ".env"
    assert env_file.exists()
    content = env_file.read_text()
    assert "PATH=" not in content
    assert "# ANTHROPIC_API_KEY=sk-ant-..." in content
    assert "# OPENAI_API_KEY=sk-..." in content
    assert "# GEMINI_API_KEY=..." in content
    assert "# GOOGLE_API_KEY=..." in content
    assert "# LITELLM_LOG=DEBUG" in content
    mode = env_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_generate_plist_embeds_path() -> None:
    plist = _generate_plist("/usr/local/bin/operator")
    assert "<key>EnvironmentVariables</key>" in plist
    assert "<key>PATH</key>" in plist
    assert "/usr" in plist or "/bin" in plist


def test_generate_systemd_unit_embeds_path() -> None:
    unit = _generate_systemd_unit("/usr/local/bin/operator")
    assert "Environment=PATH=" in unit
    assert "/usr" in unit or "/bin" in unit


def test_service_start_bootstraps_launchd_agent_when_unloaded(tmp_path: Path) -> None:
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


def test_service_stop_boots_out_launchd_agent_on_macos() -> None:
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


def test_service_restart_kickstarts_loaded_launchd_agent_on_macos(tmp_path: Path) -> None:
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


def test_init_skips_existing_env_file(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()
    env_file = op_dir / ".env"
    env_file.write_text("EXISTING=yes\n")

    with patch("operator_ai.cli.OPERATOR_DIR", op_dir):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert env_file.read_text() == "EXISTING=yes\n"


def test_init_prompts_before_overwriting_existing_config(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()
    config_file = op_dir / "operator.yaml"
    config_file.write_text("defaults:\n  models:\n    - custom/model\n")

    with patch("operator_ai.cli.OPERATOR_DIR", op_dir):
        result = runner.invoke(app, ["init"], input="n\n")

    assert result.exit_code == 0
    assert "Overwrite operator.yaml?" in result.output
    assert "remains unchanged" in result.output
    assert config_file.read_text() == "defaults:\n  models:\n    - custom/model\n"


def test_init_overwrites_existing_config_when_confirmed_and_preserves_agent_prompt(
    tmp_path: Path,
) -> None:
    op_dir = tmp_path / ".operator"
    agent_md = op_dir / "agents" / "operator" / "AGENT.md"
    agent_md.parent.mkdir(parents=True, exist_ok=True)
    agent_md.write_text("custom agent\n")
    (op_dir / "operator.yaml").write_text("defaults:\n  models:\n    - custom/model\n")

    with patch("operator_ai.cli.OPERATOR_DIR", op_dir):
        result = runner.invoke(app, ["init"], input="y\n")

    assert result.exit_code == 0
    assert '    - "anthropic/claude-sonnet-4-6"' in (op_dir / "operator.yaml").read_text()
    assert agent_md.read_text() == "custom agent\n"


def test_init_force_overwrites_existing_config_without_prompt(tmp_path: Path) -> None:
    op_dir = tmp_path / ".operator"
    op_dir.mkdir()
    config_file = op_dir / "operator.yaml"
    config_file.write_text("defaults:\n  models:\n    - custom/model\n")

    with patch("operator_ai.cli.OPERATOR_DIR", op_dir):
        result = runner.invoke(app, ["init", "--force"])

    assert result.exit_code == 0
    assert "Overwrite operator.yaml?" not in result.output
    assert '    - "anthropic/claude-sonnet-4-6"' in config_file.read_text()
