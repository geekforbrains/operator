from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from operator_ai.config import OPERATOR_DIR
from operator_ai.job_specs import find_job_spec, scan_job_specs
from operator_ai.prompts import load_prompt

console = Console()
logger = logging.getLogger("operator.cli")

app = typer.Typer(add_completion=False)
kv_app = typer.Typer(help="Key-value store operations.")
job_app = typer.Typer(help="Job inspection and management.")
service_app = typer.Typer(help="Manage the operator background service.")
memory_app = typer.Typer(help="Browse and inspect memories.")
skill_app = typer.Typer(help="Manage skills.")
user_app = typer.Typer(help="Manage users, identities, and roles.")
app.add_typer(kv_app, name="kv")
app.add_typer(job_app, name="job")
app.add_typer(service_app, name="service")
app.add_typer(memory_app, name="memories")
app.add_typer(skill_app, name="skills")
app.add_typer(user_app, name="user")

LOG_DIR = OPERATOR_DIR / "logs"
LOG_FILE = LOG_DIR / "operator.log"

# ── Service constants ────────────────────────────────────────

_LAUNCHD_LABEL = "ai.operator"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
_SYSTEMD_UNIT = "operator.service"
_SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_PATH = _SYSTEMD_DIR / _SYSTEMD_UNIT


def _setup_cli_logging() -> None:
    """Set up logging for CLI commands — writes to the shared log file + stderr."""
    import logging.handlers

    from operator_ai.log_context import RunContextFilter

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(run_ctx)s%(message)s", datefmt="%H:%M:%S"
    )
    ctx_filter = RunContextFilter()

    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    fh.addFilter(ctx_filter)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    sh.addFilter(ctx_filter)

    root = logging.getLogger("operator")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(sh)

    for name in ("httpx", "httpcore", "litellm", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _resolve_agent(agent: str | None) -> str:
    """Resolve agent: --agent flag > OPERATOR_AGENT env > config default."""
    if agent:
        return agent
    from_env = os.environ.get("OPERATOR_AGENT")
    if from_env:
        return from_env
    try:
        from operator_ai.config import load_config

        return load_config().default_agent()
    except SystemExit:
        typer.echo("Error: no --agent flag, OPERATOR_AGENT not set, config not found.", err=True)
        raise typer.Exit(code=1) from None


def _store():
    from operator_ai.store import get_store

    return get_store()


def _is_macos() -> bool:
    return sys.platform == "darwin"


# ── Init command ──────────────────────────────────────────────

_STARTER_CONFIG = """\
# Operator configuration
# Docs: https://github.com/gavinballard/operator

defaults:
  # Model fallback chain — first model is preferred, rest are fallbacks.
  # Uses LiteLLM format: provider/model-name
  models:
    - "anthropic/claude-sonnet-4-6"
  max_iterations: 25
  context_ratio: 0.5
  # timezone: "America/Vancouver" # IANA timezone (default: UTC)
  # env_file: "~/.env"           # Load API keys from a dotenv file

agents:
  operator:
    transport:
      type: slack
      bot_token_env: SLACK_BOT_TOKEN
      app_token_env: SLACK_APP_TOKEN

roles:
  guest:
    agents: []

settings:
  reject_response: ignore   # "announce" or "ignore"

# memory:
#   embed_model: "openai/text-embedding-3-small"
#   embed_dimensions: 1536
#   harvester:
#     enabled: true
#     schedule: "*/30 * * * *"
#     model: "openai/gpt-4.1-mini"
#   cleaner:
#     enabled: true
#     schedule: "0 3 * * *"
#     model: "openai/gpt-4.1-mini"
"""


@app.command("init")
def init() -> None:
    """Scaffold the ~/.operator directory with starter config."""
    home = OPERATOR_DIR
    config_file = home / "operator.yaml"

    if config_file.exists():
        console.print(f"[bold]{config_file}[/bold] already exists — nothing to do.")
        return

    # Directories
    dirs = [
        home / "logs",
        home / "state",
        home / "agents" / "operator" / "workspace",
        home / "jobs",
        home / "skills",
        home / "shared",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        console.print(f"  [dim]created[/dim] {d}/")

    # Files — only write if not already present
    files: list[tuple[Path, str]] = [
        (config_file, _STARTER_CONFIG),
        (home / "SYSTEM.md", load_prompt("system.md")),
        (home / "agents" / "operator" / "AGENT.md", load_prompt("agent.md")),
    ]
    for path, content in files:
        if path.exists():
            console.print(f"  [yellow]exists[/yellow] {path}")
        else:
            path.write_text(content)
            console.print(f"  [green]wrote[/green]  {path}")

    # Install bundled skills
    from operator_ai.skills import install_bundled_skills

    installed = install_bundled_skills(home / "skills")
    for skill_name in installed:
        console.print(f"  [green]skill[/green]  {skill_name}")

    console.print(f"\n[bold green]Operator initialized at {home}[/bold green]")
    console.print("Edit [bold]operator.yaml[/bold] to configure your agents and transports.")
    console.print(
        "Add yourself: [bold]operator user add <username> --role admin <transport> <id>[/bold]"
    )


# ── Default: start the service ───────────────────────────────


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Operator - local AI agent runtime."""
    if ctx.invoked_subcommand is None:
        from operator_ai.main import async_main

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
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{LOG_DIR / "operator.log"}</string>
            <key>StandardErrorPath</key>
            <string>{LOG_DIR / "operator.log"}</string>
            <key>WorkingDirectory</key>
            <string>{Path.home()}</string>
        </dict>
        </plist>""")


def _generate_systemd_unit(bin_path: str) -> str:
    return textwrap.dedent(f"""\
        [Unit]
        Description=Operator local AI agent runtime

        [Service]
        ExecStart={bin_path}
        Restart=on-failure
        RestartSec=5
        StandardOutput=append:{LOG_DIR / "operator.log"}
        StandardError=append:{LOG_DIR / "operator.log"}
        WorkingDirectory={Path.home()}

        [Install]
        WantedBy=default.target""")


@service_app.command("install")
def service_install() -> None:
    """Generate and load a service definition (launchd/systemd)."""
    bin_path = _find_operator_bin()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _is_macos():
        _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Unload any existing service first to avoid duplicate processes
        if _PLIST_PATH.exists():
            subprocess.run(
                ["launchctl", "unload", str(_PLIST_PATH)],
                capture_output=True,
            )
        _PLIST_PATH.write_text(_generate_plist(bin_path))
        subprocess.run(["launchctl", "load", str(_PLIST_PATH)], check=True)
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
            subprocess.run(["launchctl", "unload", str(_PLIST_PATH)], check=False)
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
        subprocess.run(["launchctl", "start", _LAUNCHD_LABEL], check=True)
    else:
        subprocess.run(["systemctl", "--user", "start", _SYSTEMD_UNIT], check=True)
    print("Service started.")


@service_app.command("stop")
def service_stop() -> None:
    """Stop the background service."""
    if _is_macos():
        subprocess.run(["launchctl", "stop", _LAUNCHD_LABEL], check=True)
    else:
        subprocess.run(["systemctl", "--user", "stop", _SYSTEMD_UNIT], check=True)
    print("Service stopped.")


@service_app.command("restart")
def service_restart() -> None:
    """Restart the background service."""
    if _is_macos():
        subprocess.run(["launchctl", "stop", _LAUNCHD_LABEL], check=False)
        subprocess.run(["launchctl", "start", _LAUNCHD_LABEL], check=True)
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


# ── KV commands ──────────────────────────────────────────────


@kv_app.command("get")
def kv_get(
    key: str = typer.Argument(help="Key to look up."),
    agent: str | None = typer.Option(None, "--agent", "-a", help="Agent name."),
    ns: str = typer.Option("", "--ns", "-n", help="Namespace."),
) -> None:
    """Get a value from the KV store."""
    value = _store().kv_get(_resolve_agent(agent), key, ns=ns)
    if value is None:
        raise typer.Exit(code=1)
    print(value)


@kv_app.command("set")
def kv_set(
    key: str = typer.Argument(help="Key to store."),
    value: str = typer.Argument(help="Value to store."),
    agent: str | None = typer.Option(None, "--agent", "-a", help="Agent name."),
    ns: str = typer.Option("", "--ns", "-n", help="Namespace."),
    ttl: int | None = typer.Option(None, "--ttl", help="Auto-expire after N hours."),
) -> None:
    """Set a key-value pair."""
    _store().kv_set(_resolve_agent(agent), key, value, ns=ns, ttl_hours=ttl)
    print("OK")


@kv_app.command("delete")
def kv_delete(
    key: str = typer.Argument(help="Key to delete."),
    agent: str | None = typer.Option(None, "--agent", "-a", help="Agent name."),
    ns: str = typer.Option("", "--ns", "-n", help="Namespace."),
) -> None:
    """Delete a key from the KV store."""
    if not _store().kv_delete(_resolve_agent(agent), key, ns=ns):
        raise typer.Exit(code=1)
    print("OK")


@kv_app.command("list")
def kv_list(
    agent: str | None = typer.Option(None, "--agent", "-a", help="Agent name."),
    ns: str = typer.Option("", "--ns", "-n", help="Namespace."),
    prefix: str = typer.Option("", "--prefix", "-p", help="Key prefix filter."),
) -> None:
    """List keys in the KV store (JSON output)."""
    print(json.dumps(_store().kv_list(_resolve_agent(agent), ns=ns, prefix=prefix), indent=2))


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
    store = _store()
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
        last = state.last_run[:19] if state.last_run else "never"
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

    state = _store().load_job_state(name)
    enabled = Text("yes", style="green") if job.enabled else Text("no", style="red")

    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("Key", style="bold", min_width=12)
    table.add_column("Value")
    table.add_row("Name", job.name)
    table.add_row("Schedule", job.schedule)
    table.add_row("Enabled", enabled)
    table.add_row("Description", job.description or "-")
    table.add_row("Path", Text(job.path, style="dim"))

    console.print(table)
    console.print()

    result_style = {"success": "green", "error": "red", "gated": "yellow"}.get(
        state.last_result, "dim"
    )
    rt = Table(title="Runtime State", show_header=False, show_edge=False, pad_edge=False, box=None)
    rt.add_column("Key", style="bold", min_width=12)
    rt.add_column("Value")
    rt.add_row("Last run", state.last_run[:19] if state.last_run else "never")
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
    from operator_ai.config import load_config
    from operator_ai.jobs import run_job_now
    from operator_ai.store import get_store
    from operator_ai.transport.cli import CliTransport

    _setup_cli_logging()
    cli_logger = logging.getLogger("operator.cli")

    config = load_config()
    store = get_store()

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
                name=name, config=config, store=store, transports={agent_name: transport}
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
    from operator_ai.skills import rewrite_frontmatter

    jobs_dir = OPERATOR_DIR / "jobs"
    job_md = jobs_dir / name / "JOB.md"
    if not job_md.exists():
        # Try matching by frontmatter name
        job = _find_job(name)
        if job:
            job_md = Path(job.path)
        else:
            print(f"Job '{name}' not found.")
            raise typer.Exit(code=1)

    if not rewrite_frontmatter(job_md, {"enabled": enabled}):
        print(f"Failed to update frontmatter in {job_md}")
        raise typer.Exit(code=1)

    action = "Enabled" if enabled else "Disabled"
    print(f"{action} job '{name}'.")


# ── Memory commands ──────────────────────────────────────────


@memory_app.callback(invoke_without_command=True)
def memories_main(
    ctx: typer.Context,
    scope: str | None = typer.Option(None, "--scope", "-s", help="Filter by scope."),
    scope_id: str | None = typer.Option(None, "--scope-id", "-i", help="Filter by scope_id."),
    pinned: bool = typer.Option(False, "--pinned", help="Show only pinned memories."),
    limit: int = typer.Option(50, "--limit", "-n", help="Number to show."),
) -> None:
    """List memories."""
    if ctx.invoked_subcommand is not None:
        return

    store = _store()

    if pinned and scope and scope_id:
        rows = store.get_pinned_memories(scope, scope_id)
    elif pinned:
        # Get pinned across all scopes
        rows = store.list_memories(scope=scope, scope_id=scope_id, limit=limit)
        rows = [r for r in rows if r["pinned"]]
    else:
        rows = store.list_memories(scope=scope, scope_id=scope_id, limit=limit)

    if not rows:
        console.print("No memories found.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Scope")
    table.add_column("Content")
    table.add_column("", width=1)  # pin marker
    for row in rows:
        content = row["content"].replace("\n", " ")
        if len(content) > 100:
            content = content[:97] + "..."
        pin = Text("\u2691", style="yellow") if row["pinned"] else Text("")
        table.add_row(
            str(row["id"]),
            Text(f"{row['scope']}/{row['scope_id']}", style="cyan"),
            content,
            pin,
        )
    console.print(table)


@memory_app.command("stats")
def memories_stats() -> None:
    """Show memory counts per scope."""
    store = _store()
    rows = store.count_all_memories_by_scope()
    if not rows:
        console.print("No memories.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Scope", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Pinned", justify="right", style="yellow")

    total = 0
    total_pinned = 0
    for row in rows:
        count = row["count"]
        pinned_count = row["pinned"] or 0
        total += count
        total_pinned += pinned_count
        table.add_row(
            f"{row['scope']}/{row['scope_id']}",
            str(count),
            str(pinned_count) if pinned_count else "",
        )
    table.add_section()
    table.add_row(Text("Total", style="bold"), Text(str(total), style="bold"), str(total_pinned))
    console.print(table)


# ── Config command ───────────────────────────────────────────


@app.command("config")
def show_config() -> None:
    """Print the resolved configuration."""
    from operator_ai.config import load_config

    config = load_config()
    output = json.dumps(config.model_dump(), indent=2)
    console.print(Syntax(output, "json", theme="monokai"))


# ── Agents command ───────────────────────────────────────────


@app.command("agents")
def show_agents() -> None:
    """List configured agents."""
    from operator_ai.agents import scan_agents
    from operator_ai.config import load_config

    config = load_config()
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
    from operator_ai.skills import scan_skills

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


@skill_app.command("reset")
def skills_reset(
    name: str = typer.Argument(None, help="Bundled skill name to reset."),
    all_skills: bool = typer.Option(False, "--all", help="Reset all bundled skills."),
) -> None:
    """Reset bundled skill(s) to their original version."""
    from operator_ai.skills import list_bundled_skill_names, reset_bundled_skill

    skills_dir = OPERATOR_DIR / "skills"
    bundled = list_bundled_skill_names()

    if not bundled:
        console.print("No bundled skills available.")
        raise typer.Exit()

    if name is None and not all_skills:
        console.print("Available bundled skills:\n")
        for n in bundled:
            console.print(f"  {n}")
        console.print("\nUsage: [bold]operator skills reset <name>[/bold] or [bold]--all[/bold]")
        raise typer.Exit()

    targets = bundled if all_skills else [name]
    for n in targets:
        if not reset_bundled_skill(n, skills_dir):
            console.print(f"'{n}' is not a bundled skill.", style="red")
            raise typer.Exit(code=1)
        console.print(f"  [green]reset[/green] {n}")

    if all_skills:
        console.print(f"\nReset {len(targets)} bundled skill(s).")


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
            from operator_ai.config import load_config

            config = load_config()
            if role not in config.roles:
                console.print(f"[yellow]Warning:[/yellow] role '{role}' is not defined in config.")
        except Exception:
            pass

    store = _store()
    platform_id = f"{transport}:{external_id}"
    try:
        store.add_user(username)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except Exception:
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
    store = _store()
    if store.get_user(username) is None:
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)

    platform_id = f"{transport}:{external_id}"
    try:
        store.add_identity(username, platform_id)
    except Exception:
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
    store = _store()
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
    store = _store()
    if not store.remove_user(username):
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)
    console.print(f"User '{username}' removed.")


@user_app.command("list")
def user_list() -> None:
    """List all users with identities and roles."""
    store = _store()
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
    store = _store()
    user = store.get_user(username)
    if user is None:
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)

    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("Key", style="bold", min_width=12)
    table.add_column("Value")
    table.add_row("Username", user.username)
    table.add_row("Created", user.created_at)
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
    store = _store()
    if store.get_user(username) is None:
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)

    try:
        store.add_role(username, role)
    except Exception:
        console.print(f"[red]Error:[/red] user '{username}' already has role '{role}'.")
        raise typer.Exit(code=1) from None
    console.print(f"Added role '{role}' to user '{username}'.")


@user_app.command("remove-role")
def user_remove_role(
    username: str = typer.Argument(help="Username."),
    role: str = typer.Argument(help="Role to remove."),
) -> None:
    """Remove a role from a user."""
    store = _store()
    if not store.remove_role(username, role):
        console.print(f"[red]Error:[/red] user '{username}' does not have role '{role}'.")
        raise typer.Exit(code=1)
    console.print(f"Removed role '{role}' from user '{username}'.")


# ── Tools command ────────────────────────────────────────────


@app.command("tools")
def show_tools() -> None:
    """List all registered built-in tools."""
    import operator_ai.tools  # noqa: F401
    from operator_ai.tools.registry import get_tools

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
