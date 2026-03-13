from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
import yaml
from rich.console import Console

from operator_ai.config import OPERATOR_DIR, load_config
from operator_ai.layout import ensure_layout
from operator_ai.prompts import load_prompt

console = Console()

_DEFAULT_AGENT_NAME = "operator"
_DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"


@dataclass(frozen=True)
class ScaffoldResult:
    home: Path
    config_file: Path
    env_file: Path
    wrote_config: bool
    wrote_env_file: bool


def _build_starter_config(*, default_model: str = _DEFAULT_MODEL) -> str:
    transport_data: dict[str, object] = {
        "type": "slack",
        "env": {
            "bot_token": "SLACK_BOT_TOKEN",
            "app_token": "SLACK_APP_TOKEN",
        },
        "settings": {
            "include_archived_channels": False,
            "inject_channels_into_prompt": True,
            "inject_users_into_prompt": True,
            "expand_mentions": True,
        },
    }
    transport_block = yaml.safe_dump(transport_data, sort_keys=False).rstrip().splitlines()

    lines = [
        "# Operator configuration",
        "# Review and edit this file before your first run.",
        "# Docs: https://operator.geekforbrains.com",
        "# Repo: https://github.com/geekforbrains/operator",
        "",
        "runtime:",
        '  env_file: ".env"',
        "  show_usage: false",
        "  # How an agent responds when messaged from an unknown user.",
        "  # - announce: responds with a simple message",
        "  # - ignore: does not respond at all",
        "  reject_response: ignore",
        "",
        "defaults:",
        "  # Model fallback chain",
        "  # First model is preferred, the rest are fallbacks.",
        "  # Uses LiteLLM format: provider/model-name",
        "  models:",
        f'    - "{default_model}"',
        '    # - "some-provider/some-other-model"',
        '  thinking: "off"',
        "  max_iterations: 25",
        "  context_ratio: 0.5",
        "  hook_timeout: 30",
        "",
        "# Permission groups — clusters of related tools that can be referenced",
        "# as @groupname in agent permissions. Modify, split, or extend as needed.",
        "permission_groups:",
        "  memory:",
        "    - save_rule",
        "    - save_note",
        "    - search_notes",
        "    - list_rules",
        "    - list_notes",
        "    - read_note",
        "    - forget_rule",
        "    - forget_note",
        "  files:",
        "    - read_file",
        "    - write_file",
        "    - list_files",
        "  messaging:",
        "    - send_message",
        "    - send_file",
        "  skills:",
        "    - create_skill",
        "    - update_skill",
        "    - delete_skill",
        "    - list_skills",
        "    - read_skill",
        "    - run_skill",
        "  jobs:",
        "    - create_job",
        "    - update_job",
        "    - delete_job",
        "    - enable_job",
        "    - disable_job",
        "    - list_jobs",
        "  state:",
        "    - get_state",
        "    - set_state",
        "    - append_state",
        "    - pop_state",
        "    - delete_state",
        "    - list_state",
        "  shell:",
        "    - run_shell",
        "  web:",
        "    - web_fetch",
        "  users:",
        "    - manage_users",
        "    - set_timezone",
        "  agents:",
        "    - spawn_agent",
        "",
        "agents:",
        f"  {_DEFAULT_AGENT_NAME}:",
        "    permissions:",
        '      tools: "*"',
        '      skills: "*"',
        "    transport:",
    ]
    lines.extend(f"      {line}" for line in transport_block)
    lines.extend(
        [
            "",
            "roles:",
            "  guest:",
            "    agents: []",
        ]
    )
    return "\n".join(lines) + "\n"


_STARTER_CONFIG = _build_starter_config()


def _scaffold_operator_home(
    home: Path,
    *,
    config_text: str = _STARTER_CONFIG,
    overwrite_config: bool = False,
    emit_output: bool = True,
) -> ScaffoldResult:
    config_file = home / "operator.yaml"

    dirs = [
        home / "logs",
        home / "agents" / _DEFAULT_AGENT_NAME / "workspace",
        home / "jobs",
        home / "skills",
        home / "shared",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        if emit_output:
            console.print(f"  [dim]created[/dim] {d}/")

    env_file = home / ".env"
    wrote_env_file = False
    if env_file.exists():
        if emit_output:
            console.print(f"  [yellow]exists[/yellow] {env_file}")
    else:
        env_content = (
            "# Operator environment file\n"
            "# API keys and environment variables for the operator service.\n"
            "# These are defaults — existing shell environment takes precedence.\n"
            "\n"
            "# API keys — uncomment and fill in as needed:\n"
            "# ANTHROPIC_API_KEY=sk-ant-...\n"
            "# OPENAI_API_KEY=sk-...\n"
            "# GEMINI_API_KEY=...\n"
            "# GOOGLE_API_KEY=...  # Alternative name LiteLLM also accepts for Gemini\n"
            "# LITELLM_LOG=DEBUG   # Optional: include LiteLLM debug logs in operator.log\n"
        )
        env_file.write_text(env_content)
        env_file.chmod(0o600)
        wrote_env_file = True
        if emit_output:
            console.print(f"  [green]wrote[/green]  {env_file}")

    wrote_config = False
    config_mode = "updated" if overwrite_config and config_file.exists() else "wrote"
    files: list[tuple[Path, str]] = [
        (config_file, config_text),
        (home / "SYSTEM.md", load_prompt("system.md")),
        (
            home / "agents" / _DEFAULT_AGENT_NAME / "AGENT.md",
            load_prompt("agent.md")
            .replace("{name}", _DEFAULT_AGENT_NAME)
            .replace("{name_title}", _DEFAULT_AGENT_NAME.capitalize()),
        ),
    ]
    for path, content in files:
        should_write = not path.exists() or (path == config_file and overwrite_config)
        if not should_write:
            if emit_output:
                console.print(f"  [yellow]exists[/yellow] {path}")
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if path == config_file:
            wrote_config = True
        if emit_output:
            verb = config_mode if path == config_file else "wrote"
            console.print(f"  [green]{verb}[/green]  {path}")

    return ScaffoldResult(
        home=home,
        config_file=config_file,
        env_file=env_file,
        wrote_config=wrote_config,
        wrote_env_file=wrote_env_file,
    )


def init_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite operator.yaml without prompting. Existing .env and AGENT.md files are preserved.",
    ),
) -> None:
    """Scaffold ~/.operator with the default Slack config and full layout."""
    config_file = OPERATOR_DIR / "operator.yaml"
    overwrite_config = force
    if config_file.exists() and not force:
        overwrite_config = typer.confirm(
            (
                f"{config_file} already exists.\n"
                "Overwrite operator.yaml? Existing .env and AGENT.md files will be preserved, "
                "and any missing directories will be created."
            ),
            default=False,
        )
        if not overwrite_config:
            console.print(f"[yellow]Skipped[/yellow] {config_file} remains unchanged.")
            raise typer.Exit(code=0)

    result = _scaffold_operator_home(
        OPERATOR_DIR,
        config_text=_build_starter_config(),
        overwrite_config=overwrite_config,
    )
    config = load_config(result.config_file)
    ensure_layout(config)

    console.print(f"\n[bold green]Operator initialized at {result.home}[/bold green]")
    console.print(
        f"Edit [bold]{result.config_file}[/bold] to review the Slack config and model defaults."
    )
    console.print(f"Add secrets to [bold]{result.env_file}[/bold].")
    console.print(
        "Then create an admin user: [bold]operator user add <username> --role admin slack <YOUR_SLACK_USER_ID>[/bold]"
    )
