from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import operator_ai.tools  # noqa: F401
from operator_ai.agents import scan_agents
from operator_ai.config import LOGS_DIR, OPERATOR_DIR, ConfigError, load_config
from operator_ai.frontmatter import rewrite_frontmatter
from operator_ai.job_specs import find_job_spec, scan_job_specs
from operator_ai.jobs import run_job_now
from operator_ai.layout import ensure_layout
from operator_ai.log_context import setup_logging
from operator_ai.main import async_main
from operator_ai.memory import MemoryStore
from operator_ai.memory_index import MemoryIndex
from operator_ai.memory_reindex import reindex_diff, reindex_full
from operator_ai.message_timestamps import format_ts
from operator_ai.prompts import load_prompt
from operator_ai.skills import scan_skills
from operator_ai.store import get_store
from operator_ai.tools.registry import get_tools
from operator_ai.transport.cli import CliTransport
from operator_ai.transport.registry import transport_logger_names

console = Console()
logger = logging.getLogger("operator.cli")

app = typer.Typer(add_completion=False)
job_app = typer.Typer(help="Job inspection and management.")
service_app = typer.Typer(help="Manage the operator background service.")
memory_app = typer.Typer(help="Browse and search memories.")
skill_app = typer.Typer(help="Manage skills.")
user_app = typer.Typer(help="Manage users, identities, and roles.")
app.add_typer(job_app, name="job")
app.add_typer(service_app, name="service")
app.add_typer(memory_app, name="memory")
app.add_typer(skill_app, name="skills")
app.add_typer(user_app, name="user")

LOG_FILE = LOGS_DIR / "operator.log"

# ── Service constants ────────────────────────────────────────

_LAUNCHD_LABEL = "ai.operator"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
_SYSTEMD_UNIT = "operator.service"
_SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_PATH = _SYSTEMD_DIR / _SYSTEMD_UNIT


def _launchd_domain_target() -> str:
    return f"gui/{os.getuid()}"


def _launchd_service_target() -> str:
    return f"{_launchd_domain_target()}/{_LAUNCHD_LABEL}"


def _launchd_service_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "list", _LAUNCHD_LABEL],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _require_launchd_plist() -> None:
    if not _PLIST_PATH.exists():
        print("Service not installed.")
        raise typer.Exit(code=1)


def _setup_cli_logging() -> None:
    """Set up logging for CLI commands — writes to the shared log file + stderr."""
    setup_logging(
        log_dir=LOGS_DIR,
        stderr=True,
        noisy_loggers=("httpx", "httpcore", "litellm", "openai", *transport_logger_names()),
    )


def _resolve_agent(agent: str | None) -> str:
    """Resolve agent: --agent flag > OPERATOR_AGENT env > config default."""
    if agent:
        return agent
    from_env = os.environ.get("OPERATOR_AGENT")
    if from_env:
        return from_env
    try:
        return load_config().default_agent()
    except ConfigError:
        typer.echo("Error: no --agent flag, OPERATOR_AGENT not set, config not found.", err=True)
        raise typer.Exit(code=1) from None


def _is_macos() -> bool:
    return sys.platform == "darwin"


# ── Init command ──────────────────────────────────────────────

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

    # Directories
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


@app.command("init")
def init(
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
    console.print(f"Edit [bold]{result.config_file}[/bold] to review the Slack config and model defaults.")
    console.print(f"Add secrets to [bold]{result.env_file}[/bold].")
    console.print(
        "Then create an admin user: [bold]operator user add <username> --role admin slack <YOUR_SLACK_USER_ID>[/bold]"
    )


# ── Default: start the service ───────────────────────────────


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Operator - local AI agent runtime."""
    if ctx.invoked_subcommand is None:
        asyncio.run(async_main())


# ── Service commands ─────────────────────────────────────────


def _find_operator_bin() -> str:
    """Find the operator executable path."""
    path = shutil.which("operator")
    if path:
        return path
    # Fallback: assume it's the current Python's entry point
    return str(Path(sys.executable).parent / "operator")


def _generate_plist(bin_path: str) -> str:
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{bin_path}</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>{current_path}</string>
            </dict>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{LOGS_DIR / "operator.log"}</string>
            <key>StandardErrorPath</key>
            <string>{LOGS_DIR / "operator.log"}</string>
            <key>WorkingDirectory</key>
            <string>{Path.home()}</string>
        </dict>
        </plist>""")


def _generate_systemd_unit(bin_path: str) -> str:
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    return textwrap.dedent(f"""\
        [Unit]
        Description=Operator local AI agent runtime

        [Service]
        ExecStart={bin_path}
        Environment=PATH={current_path}
        Restart=on-failure
        RestartSec=5
        StandardOutput=append:{LOGS_DIR / "operator.log"}
        StandardError=append:{LOGS_DIR / "operator.log"}
        WorkingDirectory={Path.home()}

        [Install]
        WantedBy=default.target""")


@service_app.command("install")
def service_install() -> None:
    """Generate and load a service definition (launchd/systemd)."""
    bin_path = _find_operator_bin()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if _is_macos():
        _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _launchd_service_loaded():
            subprocess.run(["launchctl", "bootout", _launchd_service_target()], check=False)
        _PLIST_PATH.write_text(_generate_plist(bin_path))
        subprocess.run(
            ["launchctl", "bootstrap", _launchd_domain_target(), str(_PLIST_PATH)],
            check=True,
        )
        print(f"Installed and loaded {_PLIST_PATH}")
    else:
        _SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
        _SYSTEMD_PATH.write_text(_generate_systemd_unit(bin_path))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", _SYSTEMD_UNIT], check=True)
        print(f"Installed and enabled {_SYSTEMD_PATH}")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Unload and remove the service definition."""
    if _is_macos():
        if _PLIST_PATH.exists():
            if _launchd_service_loaded():
                subprocess.run(["launchctl", "bootout", _launchd_service_target()], check=False)
            _PLIST_PATH.unlink()
            print(f"Unloaded and removed {_PLIST_PATH}")
        else:
            print("Service not installed.")
    else:
        subprocess.run(["systemctl", "--user", "disable", _SYSTEMD_UNIT], check=False)
        subprocess.run(["systemctl", "--user", "stop", _SYSTEMD_UNIT], check=False)
        if _SYSTEMD_PATH.exists():
            _SYSTEMD_PATH.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            print(f"Removed {_SYSTEMD_PATH}")
        else:
            print("Service not installed.")


@service_app.command("start")
def service_start() -> None:
    """Start the background service."""
    if _is_macos():
        _require_launchd_plist()
        if _launchd_service_loaded():
            subprocess.run(["launchctl", "kickstart", _launchd_service_target()], check=True)
        else:
            subprocess.run(
                ["launchctl", "bootstrap", _launchd_domain_target(), str(_PLIST_PATH)],
                check=True,
            )
    else:
        subprocess.run(["systemctl", "--user", "start", _SYSTEMD_UNIT], check=True)
    print("Service started.")


@service_app.command("stop")
def service_stop() -> None:
    """Stop the background service."""
    if _is_macos():
        if not _launchd_service_loaded():
            print("Service already stopped.")
            return
        subprocess.run(["launchctl", "bootout", _launchd_service_target()], check=True)
    else:
        subprocess.run(["systemctl", "--user", "stop", _SYSTEMD_UNIT], check=True)
    print("Service stopped.")


@service_app.command("restart")
def service_restart() -> None:
    """Restart the background service."""
    if _is_macos():
        _require_launchd_plist()
        if _launchd_service_loaded():
            subprocess.run(["launchctl", "kickstart", "-k", _launchd_service_target()], check=True)
        else:
            subprocess.run(
                ["launchctl", "bootstrap", _launchd_domain_target(), str(_PLIST_PATH)],
                check=True,
            )
    else:
        subprocess.run(["systemctl", "--user", "restart", _SYSTEMD_UNIT], check=True)
    print("Service restarted.")


@service_app.command("status")
def service_status() -> None:
    """Show whether the service is running."""
    if _is_macos():
        result = subprocess.run(
            ["launchctl", "list", _LAUNCHD_LABEL],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("Service not loaded.")
            raise typer.Exit(code=1)
        # Parse the dict-style output from `launchctl list <label>`
        output = result.stdout
        pid_match = re.search(r'"PID"\s*=\s*(\d+)', output)
        exit_match = re.search(r'"LastExitStatus"\s*=\s*(\d+)', output)
        last_exit = exit_match.group(1) if exit_match else "?"
        if pid_match:
            print(f"Running (PID {pid_match.group(1)}, last exit {last_exit})")
        else:
            print(f"Loaded but not running (last exit {last_exit})")
    else:
        result = subprocess.run(
            ["systemctl", "--user", "status", _SYSTEMD_UNIT],
            capture_output=True,
            text=True,
        )
        print(result.stdout.strip())
        if result.returncode != 0:
            raise typer.Exit(code=1)


# ── Logs command ─────────────────────────────────────────────


@app.command("logs")
def logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output."),
    lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show."),
) -> None:
    """Tail the operator log file."""
    if not LOG_FILE.exists():
        print(f"No log file found at {LOG_FILE}")
        raise typer.Exit(code=1)
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(LOG_FILE))
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(cmd)


# ── Job commands ─────────────────────────────────────────────


def _scan_jobs():
    """Lightweight job scan — reads frontmatter without importing the full jobs module."""
    return scan_job_specs(OPERATOR_DIR / "jobs")


def _find_job(name: str):
    return find_job_spec(name, OPERATOR_DIR / "jobs")


@job_app.command("list")
def job_list() -> None:
    """List all jobs with status."""
    jobs = _scan_jobs()
    if not jobs:
        console.print("No jobs found.")
        raise typer.Exit()
    store = get_store()
    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Status")
    table.add_column("Schedule", style="dim")
    table.add_column("Last Run")
    table.add_column("Result")
    table.add_column("Runs", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Gates", justify="right")
    table.add_column("Skips", justify="right")
    for job in jobs:
        state = store.load_job_state(job.name)
        status = Text("enabled", style="green") if job.enabled else Text("disabled", style="red")
        last = format_ts(state.last_run) if state.last_run else "never"
        result_style = {"success": "green", "error": "red", "gated": "yellow"}.get(
            state.last_result, "dim"
        )
        result = Text(state.last_result or "-", style=result_style)
        errors = (
            Text(str(state.error_count), style="red")
            if state.error_count
            else Text("0", style="dim")
        )
        gates = (
            Text(str(state.gate_count), style="yellow")
            if state.gate_count
            else Text("0", style="dim")
        )
        skips = (
            Text(str(state.skip_count), style="yellow")
            if state.skip_count
            else Text("0", style="dim")
        )
        table.add_row(
            job.name,
            status,
            job.schedule,
            last,
            result,
            str(state.run_count),
            errors,
            gates,
            skips,
        )
    console.print(table)


@job_app.command("info")
def job_info(
    name: str = typer.Argument(help="Job name."),
) -> None:
    """Show job configuration and runtime state."""
    job = _find_job(name)
    if not job:
        console.print(f"Job '{name}' not found.", style="red")
        raise typer.Exit(code=1)

    state = get_store().load_job_state(name)
    enabled = Text("yes", style="green") if job.enabled else Text("no", style="red")

    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("Key", style="bold", min_width=12)
    table.add_column("Value")
    table.add_row("Name", job.name)
    table.add_row("Schedule", job.schedule)
    table.add_row("Enabled", enabled)
    table.add_row("Description", job.description or "-")
    table.add_row("Path", Text(str(job.path), style="dim"))

    console.print(table)
    console.print()

    result_style = {"success": "green", "error": "red", "gated": "yellow"}.get(
        state.last_result, "dim"
    )
    rt = Table(title="Runtime State", show_header=False, show_edge=False, pad_edge=False, box=None)
    rt.add_column("Key", style="bold", min_width=12)
    rt.add_column("Value")
    rt.add_row("Last run", format_ts(state.last_run) if state.last_run else "never")
    rt.add_row("Last result", Text(state.last_result or "-", style=result_style))
    if state.last_duration_seconds:
        rt.add_row("Duration", f"{state.last_duration_seconds}s")
    if state.last_error:
        rt.add_row("Last error", Text(state.last_error, style="red"))
    rt.add_row("Run count", str(state.run_count))
    rt.add_row("Error count", str(state.error_count))
    rt.add_row("Gate count", str(state.gate_count))
    rt.add_row("Skip count", str(state.skip_count))
    console.print(rt)


@job_app.command("run")
def job_run(
    name: str = typer.Argument(help="Job name to run immediately."),
) -> None:
    """Trigger a job immediately (outside the cron schedule)."""
    _setup_cli_logging()
    cli_logger = logging.getLogger("operator.cli")

    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    store = get_store()
    memory_store = MemoryStore(base_dir=OPERATOR_DIR)

    job = next((job for job in _scan_jobs() if job.name == name), None)
    if not job:
        print(f"Job '{name}' not found.")
        raise typer.Exit(code=1)
    agent_name = job.agent or config.default_agent()

    cli_logger.info("CLI job run: '%s' (agent: %s)", name, agent_name)
    print(f"Running job '{name}' with agent '{agent_name}'...")

    transport = CliTransport(agent_name)

    async def _run() -> None:
        try:
            await run_job_now(
                name=name,
                config=config,
                store=store,
                transports={agent_name: transport},
                memory_store=memory_store,
            )
        except ValueError as e:
            print(str(e))
            raise typer.Exit(code=1) from None

        state = store.load_job_state(name)
        result = state.last_result or "unknown"
        duration = f"{state.last_duration_seconds}s" if state.last_duration_seconds else "unknown"
        print(f"Result: {result} (duration: {duration})")
        if state.last_error:
            print(state.last_error)

    asyncio.run(_run())


@job_app.command("enable")
def job_enable(
    name: str = typer.Argument(help="Job name."),
) -> None:
    """Enable a job."""
    _toggle_job(name, enabled=True)


@job_app.command("disable")
def job_disable(
    name: str = typer.Argument(help="Job name."),
) -> None:
    """Disable a job."""
    _toggle_job(name, enabled=False)


def _toggle_job(name: str, *, enabled: bool) -> None:
    job = _find_job(name)
    if not job:
        print(f"Job '{name}' not found.")
        raise typer.Exit(code=1)
    job_md = Path(job.path)

    if not rewrite_frontmatter(job_md, {"enabled": enabled}):
        print(f"Failed to update frontmatter in {job_md}")
        raise typer.Exit(code=1)

    action = "Enabled" if enabled else "Disabled"
    print(f"{action} job '{name}'.")


# ── Memory commands ──────────────────────────────────────────


@memory_app.callback(invoke_without_command=True)
def memory_main(ctx: typer.Context) -> None:
    """Browse and search file-backed memories."""
    if ctx.invoked_subcommand is None:
        memory_list_cmd(scope="global")


@memory_app.command("list")
def memory_list_cmd(
    scope: str = typer.Argument("global", help="Scope: global, agent:<name>, or user:<name>."),
) -> None:
    """List rules and notes for a scope."""
    index_db = OPERATOR_DIR / "db" / "memory_index.db"
    index = MemoryIndex(index_db) if index_db.exists() else None
    mem = MemoryStore(base_dir=OPERATOR_DIR, index=index)
    rules = mem.list_rules(scope)
    notes = mem.list_notes(scope)

    if not rules and not notes:
        console.print(f"No memories in scope '{scope}'.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Type", style="bold")
    table.add_column("Path", style="dim")
    table.add_column("Updated", style="dim")
    table.add_column("Expires", style="dim")
    table.add_column("Content")

    for mf in rules:
        content = mf.content.replace("\n", " ")
        if len(content) > 80:
            content = content[:77] + "..."
        updated = mf.updated_at.strftime("%Y-%m-%d %H:%M") if mf.updated_at else "-"
        expires = mf.expires_at.strftime("%Y-%m-%d %H:%M") if mf.expires_at else "-"
        table.add_row("rule", mf.relative_path, updated, expires, content)

    for mf in notes:
        content = mf.content.replace("\n", " ")
        if len(content) > 80:
            content = content[:77] + "..."
        updated = mf.updated_at.strftime("%Y-%m-%d %H:%M") if mf.updated_at else "-"
        expires = mf.expires_at.strftime("%Y-%m-%d %H:%M") if mf.expires_at else "-"
        table.add_row("note", mf.relative_path, updated, expires, content)

    console.print(table)


@memory_app.command("search")
def memory_search_cmd(
    query: str = typer.Argument(help="Search query."),
    scope: str = typer.Option("global", "--scope", "-s", help="Scope to search."),
) -> None:
    """Search notes by filename and content."""
    index_db = OPERATOR_DIR / "db" / "memory_index.db"
    index = MemoryIndex(index_db) if index_db.exists() else None
    mem = MemoryStore(base_dir=OPERATOR_DIR, index=index)
    results = mem.search_notes(scope, query)

    if not results:
        console.print(f"No notes matching '{query}' in scope '{scope}'.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Path", style="dim")
    table.add_column("Updated", style="dim")
    table.add_column("Content")

    for mf in results:
        content = mf.content.replace("\n", " ")
        if len(content) > 80:
            content = content[:77] + "..."
        updated = mf.updated_at.strftime("%Y-%m-%d %H:%M") if mf.updated_at else "-"
        table.add_row(mf.relative_path, updated, content)

    console.print(table)


@memory_app.command("index")
def memory_index_cmd(
    force: bool = typer.Option(False, "--force", help="Full rebuild instead of hash-diff."),
) -> None:
    """Rebuild the FTS5 search index from memory files on disk."""
    index_db = OPERATOR_DIR / "db" / "memory_index.db"
    index = MemoryIndex(index_db)
    mem = MemoryStore(base_dir=OPERATOR_DIR, index=index)

    if force:
        count = reindex_full(mem, index)
        console.print(f"Full reindex complete: {count} files indexed.")
    else:
        upserted, deleted = reindex_diff(mem, index)
        console.print(f"Reindex complete: {upserted} updated, {deleted} removed.")

    index.close()


# ── Config command ───────────────────────────────────────────


@app.command("config")
def show_config() -> None:
    """Print the resolved configuration."""
    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    output = json.dumps(config.model_dump(), indent=2)
    console.print(Syntax(output, "json", theme="monokai"))


# ── Agents command ───────────────────────────────────────────


@app.command("agents")
def show_agents() -> None:
    """List configured agents."""
    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    if not config.agents:
        console.print("No agents configured.")
        raise typer.Exit()

    agent_infos = {a.name: a for a in scan_agents()}

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Agent", style="bold")
    table.add_column("Description")
    table.add_column("Transport")
    table.add_column("Models")
    table.add_column("Files", style="dim")

    for name, agent in config.agents.items():
        models = ", ".join(agent.models) if agent.models else ", ".join(config.defaults.models)
        transport_type = agent.transport.type if agent.transport else "none"
        info = agent_infos.get(name)
        description = info.description if info else ""
        agent_md = config.agent_prompt_path(name)
        workspace = config.agent_workspace(name)
        flags = []
        if agent_md.exists():
            flags.append("AGENT.md")
        if workspace.exists():
            flags.append("workspace/")
        table.add_row(
            name, description or "-", transport_type, models, ", ".join(flags) if flags else "-"
        )
    console.print(table)


# ── Skills commands ──────────────────────────────────────────


@skill_app.callback(invoke_without_command=True)
def skills_main(ctx: typer.Context) -> None:
    """List discovered skills."""
    if ctx.invoked_subcommand is not None:
        return
    _show_skills()


@skill_app.command("list")
def skills_list() -> None:
    """List discovered skills."""
    _show_skills()


def _show_skills() -> None:
    skills_dir = OPERATOR_DIR / "skills"
    skills = scan_skills(skills_dir)
    if not skills:
        console.print("No skills found.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Skill", style="bold")
    table.add_column("Description")
    table.add_column("Env")

    for s in skills:
        desc = s.description
        if len(desc) > 80:
            desc = desc[:77] + "..."
        if not s.env:
            env_status = Text("-", style="dim")
        elif s.env_missing:
            env_status = Text(f"missing: {', '.join(s.env_missing)}", style="red")
        else:
            env_status = Text("ok", style="green")
        table.add_row(s.name, desc, env_status)
    console.print(table)


# ── User commands ────────────────────────────────────────────


@user_app.command("add")
def user_add(
    username: str = typer.Argument(help="Username (lowercase alphanumeric, dots, hyphens)."),
    transport: str = typer.Argument(help="Transport name (e.g. slack, telegram)."),
    external_id: str = typer.Argument(help="External ID on that transport."),
    role: str = typer.Option(..., "--role", "-r", help="Role to assign."),
) -> None:
    """Create a user with an initial identity and role."""
    if role != "admin":
        try:
            config = load_config()
            if role not in config.roles:
                console.print(f"[yellow]Warning:[/yellow] role '{role}' is not defined in config.")
        except ConfigError:
            pass

    store = get_store()
    platform_id = f"{transport}:{external_id}"
    try:
        store.add_user(username)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except sqlite3.IntegrityError:
        console.print(f"[red]Error:[/red] user '{username}' already exists.")
        raise typer.Exit(code=1) from None

    store.add_role(username, role)
    store.add_identity(username, platform_id)
    console.print(f"User '{username}' created with role '{role}' and identity '{platform_id}'.")


@user_app.command("link")
def user_link(
    username: str = typer.Argument(help="Username."),
    transport: str = typer.Argument(help="Transport name."),
    external_id: str = typer.Argument(help="External ID on that transport."),
) -> None:
    """Link a transport identity to an existing user."""
    store = get_store()
    if store.get_user(username) is None:
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)

    platform_id = f"{transport}:{external_id}"
    try:
        store.add_identity(username, platform_id)
    except sqlite3.IntegrityError:
        console.print(f"[red]Error:[/red] identity '{platform_id}' already linked.")
        raise typer.Exit(code=1) from None
    console.print(f"Linked '{platform_id}' to user '{username}'.")


@user_app.command("unlink")
def user_unlink(
    username: str = typer.Argument(help="Username."),
    transport: str = typer.Argument(help="Transport name."),
    external_id: str = typer.Argument(help="External ID on that transport."),
) -> None:
    """Remove a transport identity from a user."""
    store = get_store()
    platform_id = f"{transport}:{external_id}"
    if not store.remove_identity(platform_id):
        console.print(f"[red]Error:[/red] identity '{platform_id}' not found.")
        raise typer.Exit(code=1)
    console.print(f"Unlinked '{platform_id}' from user '{username}'.")


@user_app.command("remove")
def user_remove(
    username: str = typer.Argument(help="Username to remove."),
) -> None:
    """Remove a user entirely (cascades identities and roles)."""
    store = get_store()
    if not store.remove_user(username):
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)
    console.print(f"User '{username}' removed.")


@user_app.command("list")
def user_list() -> None:
    """List all users with identities and roles."""
    store = get_store()
    users = store.list_users()
    if not users:
        console.print("No users found.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Username", style="bold")
    table.add_column("Roles")
    table.add_column("Identities")
    for user in users:
        roles = ", ".join(user.roles) if user.roles else "-"
        identities = ", ".join(user.identities) if user.identities else "-"
        table.add_row(user.username, roles, identities)
    console.print(table)


@user_app.command("info")
def user_info(
    username: str = typer.Argument(help="Username to inspect."),
) -> None:
    """Show details for one user."""
    store = get_store()
    user = store.get_user(username)
    if user is None:
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)

    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("Key", style="bold", min_width=12)
    table.add_column("Value")
    table.add_row("Username", user.username)
    table.add_row("Created", format_ts(user.created_at))
    table.add_row("Roles", ", ".join(user.roles) if user.roles else "-")
    table.add_row(
        "Identities",
        ", ".join(user.identities) if user.identities else "-",
    )
    console.print(table)


@user_app.command("add-role")
def user_add_role(
    username: str = typer.Argument(help="Username."),
    role: str = typer.Argument(help="Role to add."),
) -> None:
    """Add a role to a user."""
    store = get_store()
    if store.get_user(username) is None:
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)

    try:
        store.add_role(username, role)
    except sqlite3.IntegrityError:
        console.print(f"[red]Error:[/red] user '{username}' already has role '{role}'.")
        raise typer.Exit(code=1) from None
    console.print(f"Added role '{role}' to user '{username}'.")


@user_app.command("remove-role")
def user_remove_role(
    username: str = typer.Argument(help="Username."),
    role: str = typer.Argument(help="Role to remove."),
) -> None:
    """Remove a role from a user."""
    store = get_store()
    if not store.remove_role(username, role):
        console.print(f"[red]Error:[/red] user '{username}' does not have role '{role}'.")
        raise typer.Exit(code=1)
    console.print(f"Removed role '{role}' from user '{username}'.")


# ── Tools command ────────────────────────────────────────────


@app.command("tools")
def show_tools() -> None:
    """List all registered built-in tools."""
    tools = get_tools()
    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Tool", style="bold")
    table.add_column("Description")

    for t in sorted(tools, key=lambda t: t.name):
        desc = t.description
        if len(desc) > 80:
            desc = desc[:77] + "..."
        table.add_row(t.name, desc)

    console.print(table)
    console.print("\n[dim]Transports may provide additional tools at runtime.[/dim]")


# ── Entry point ──────────────────────────────────────────────


def cli() -> None:
    app()
