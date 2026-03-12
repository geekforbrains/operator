from __future__ import annotations

import asyncio
import contextlib
import getpass
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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import operator_ai.tools  # noqa: F401
from operator_ai.agents import scan_agents
from operator_ai.config import LOGS_DIR, OPERATOR_DIR, ConfigError, load_config, parse_env_file
from operator_ai.frontmatter import rewrite_frontmatter
from operator_ai.job_specs import find_job_spec, scan_job_specs
from operator_ai.jobs import run_job_now
from operator_ai.log_context import setup_logging
from operator_ai.main import async_main
from operator_ai.memory import MemoryStore
from operator_ai.memory_index import MemoryIndex
from operator_ai.memory_reindex import reindex_diff, reindex_full
from operator_ai.message_timestamps import format_ts
from operator_ai.prompts import load_prompt
from operator_ai.skills import scan_skills
from operator_ai.store import USERNAME_RE, get_store
from operator_ai.tools.registry import get_tools
from operator_ai.transport.cli import CliTransport
from operator_ai.transport.registry import (
    SetupSecret,
    SetupTransport,
    default_setup_transport,
    list_setup_transports,
    transport_logger_names,
)

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
_DEFAULT_PROVIDER = "anthropic"


@dataclass(frozen=True)
class SetupProvider:
    name: str
    label: str
    default_model: str
    secret: SetupSecret


@dataclass(frozen=True)
class ScaffoldResult:
    home: Path
    config_file: Path
    env_file: Path
    wrote_config: bool
    wrote_env_file: bool


@dataclass(frozen=True)
class ResolvedSecret:
    env_var: str
    value: str


_SETUP_PROVIDERS: dict[str, SetupProvider] = {
    "anthropic": SetupProvider(
        name="anthropic",
        label="Anthropic",
        default_model="anthropic/claude-sonnet-4-6",
        secret=SetupSecret(
            env_vars=("ANTHROPIC_API_KEY",),
            prompt="Anthropic API key (sk-ant-*)",
            warning_prefix="sk-ant-",
        ),
    ),
    "openai": SetupProvider(
        name="openai",
        label="OpenAI",
        default_model="openai/gpt-4.1",
        secret=SetupSecret(
            env_vars=("OPENAI_API_KEY",),
            prompt="OpenAI API key (sk-*)",
            warning_prefix="sk-",
        ),
    ),
    "gemini": SetupProvider(
        name="gemini",
        label="Gemini",
        default_model="gemini/gemini-2.5-flash",
        secret=SetupSecret(
            env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            prompt="Gemini API key",
        ),
    ),
}
_DEFAULT_MODEL = _SETUP_PROVIDERS[_DEFAULT_PROVIDER].default_model


def _build_starter_config(
    *,
    default_model: str = _DEFAULT_MODEL,
    transport_name: str | None = None,
    transport_env: dict[str, object] | None = None,
    transport_settings: dict[str, object] | None = None,
) -> str:
    selected_transport = default_setup_transport()
    resolved_transport_name = transport_name or selected_transport.name
    resolved_transport_env = transport_env or dict(selected_transport.env_defaults)
    resolved_transport_settings = transport_settings or dict(selected_transport.settings_defaults)
    transport_data: dict[str, object] = {
        "type": resolved_transport_name,
        "env": resolved_transport_env,
        "settings": resolved_transport_settings,
    }
    transport_block = yaml.safe_dump(transport_data, sort_keys=False).rstrip().splitlines()

    lines = [
        "# Operator configuration",
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
        "  # first model is preferred, rest are fallbacks.",
        "  # Uses LiteLLM format: provider/model-name",
        "  models:",
        f'    - "{default_model}"',
        '    # - "some-provider/some-other-model"',
        "  max_iterations: 50",
        "  context_ratio: 0.5",
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
        if path.exists():
            if emit_output:
                console.print(f"  [yellow]exists[/yellow] {path}")
        else:
            path.write_text(content)
            if path == config_file:
                wrote_config = True
            if emit_output:
                console.print(f"  [green]wrote[/green]  {path}")

    return ScaffoldResult(
        home=home,
        config_file=config_file,
        env_file=env_file,
        wrote_config=wrote_config,
        wrote_env_file=wrote_env_file,
    )


def _quote_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:/+\-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _, _ = line.partition("=")
        env_var = key.strip()
        if env_var in pending:
            new_lines.append(f"{env_var}={_quote_env_value(pending.pop(env_var))}")
        else:
            new_lines.append(line)

    if pending and new_lines and new_lines[-1] != "":
        new_lines.append("")
    for env_var, value in pending.items():
        new_lines.append(f"{env_var}={_quote_env_value(value)}")

    path.write_text("\n".join(new_lines).rstrip() + "\n")
    path.chmod(0o600)


def _default_setup_username() -> str:
    raw = getpass.getuser().strip().lower()
    slug = re.sub(r"[^a-z0-9.\-]+", "-", raw).strip(".-")
    return slug[:64] or "operator-admin"


def _normalize_timezone_name(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, KeyError):
        return None
    return candidate


def _timezone_from_zoneinfo_path(path: Path) -> str | None:
    try:
        parts = path.resolve().parts
    except OSError:
        return None
    matches = [i for i, part in enumerate(parts) if part == "zoneinfo"]
    if not matches:
        return None
    candidate = "/".join(parts[matches[-1] + 1 :])
    return _normalize_timezone_name(candidate)


def _detect_local_timezone() -> str:
    tz_env = _normalize_timezone_name(os.environ.get("TZ", ""))
    if tz_env:
        return tz_env

    try:
        local_tz = _normalize_timezone_name(getattr(datetime.now().astimezone().tzinfo, "key", ""))
        if local_tz:
            return local_tz
    except Exception:
        pass

    for path_str in ("/etc/localtime", "/etc/timezone"):
        path = Path(path_str)
        if not path.exists():
            continue
        if path.name == "localtime":
            timezone = _timezone_from_zoneinfo_path(path)
            if timezone:
                return timezone
            continue
        timezone = _normalize_timezone_name(path.read_text().strip())
        if timezone:
            return timezone

    timedatectl = shutil.which("timedatectl")
    if timedatectl:
        result = subprocess.run(
            [timedatectl, "show", "--property=Timezone", "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
        timezone = _normalize_timezone_name(result.stdout.strip())
        if timezone:
            return timezone

    return "UTC"


def _prompt_timezone(timezone: str | None) -> str:
    detected = _detect_local_timezone()
    if timezone is not None:
        resolved = _normalize_timezone_name(timezone)
        if resolved is None:
            raise typer.BadParameter(
                "Timezone must be a valid IANA zone like America/Vancouver.",
                param_hint="--timezone",
            )
        return resolved

    console.print(f"[dim]Detected timezone: {detected}[/dim]")
    while True:
        value = typer.prompt("Timezone", default=detected).strip()
        resolved = _normalize_timezone_name(value)
        if resolved is not None:
            return resolved
        console.print("[red]Use a valid IANA timezone like America/Vancouver.[/red]")


def _prompt_username(username: str | None) -> str:
    if username is not None:
        value = username.strip()
        if not USERNAME_RE.match(value):
            raise typer.BadParameter(
                "Username must be 1-64 chars using lowercase letters, numbers, dots, and hyphens."
            )
        return value

    default = _default_setup_username()
    while True:
        value = typer.prompt("Admin username", default=default).strip()
        if USERNAME_RE.match(value):
            return value
        console.print(
            "[red]Username must be 1-64 chars using lowercase letters, numbers, dots, and hyphens.[/red]"
        )


def _resolve_setup_transport(name: str | None) -> SetupTransport:
    if name is None:
        return default_setup_transport()
    key = name.strip().lower()
    transports = {transport.name: transport for transport in list_setup_transports()}
    transport = transports.get(key)
    if transport is None:
        available = ", ".join(sorted(transports))
        raise typer.BadParameter(f"Unknown transport {name!r}. Available: {available}")
    return transport


def _parse_secret_overrides(values: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for raw in values or []:
        key, sep, value = raw.partition("=")
        env_var = key.strip()
        if not sep or not env_var:
            raise typer.BadParameter(
                "Use --secret ENV_VAR=value.",
                param_hint="--secret",
            )
        overrides[env_var] = value
    return overrides


def _resolve_setup_provider(name: str | None) -> SetupProvider:
    if name is not None:
        key = name.strip().lower()
        provider = _SETUP_PROVIDERS.get(key)
        if provider is None:
            available = ", ".join(sorted(_SETUP_PROVIDERS))
            raise typer.BadParameter(f"Unknown provider {name!r}. Available: {available}")
        return provider

    console.print("[dim]Available providers: anthropic, openai, gemini[/dim]")
    while True:
        key = typer.prompt("Model provider", default=_DEFAULT_PROVIDER).strip().lower()
        provider = _SETUP_PROVIDERS.get(key)
        if provider is not None:
            return provider
        console.print("[red]Choose one of: anthropic, openai, gemini.[/red]")


def _resolve_secret(
    *,
    cli_value: str | None,
    secret: SetupSecret,
    env_file_values: dict[str, str],
    env_file: Path,
    force: bool = False,
) -> ResolvedSecret:
    value = cli_value.strip() if cli_value else ""
    env_var = secret.env_var
    source = "command line"
    if not value and not force:
        for candidate in secret.env_vars:
            candidate_value = env_file_values.get(candidate, "")
            if candidate_value:
                value = candidate_value
                env_var = candidate
                source = str(env_file)
                break
    if not value and not force:
        for candidate in secret.env_vars:
            candidate_value = os.environ.get(candidate, "").strip()
            if candidate_value:
                value = candidate_value
                env_var = secret.env_var
                source = "shell environment"
                break
    while not value:
        value = typer.prompt(secret.prompt, hide_input=secret.hidden).strip()
        env_var = secret.env_var
        source = "prompt"
        if not value:
            console.print(f"[red]{secret.prompt} is required.[/red]")
    if secret.warning_prefix and not value.startswith(secret.warning_prefix):
        console.print(
            f"[yellow]Warning:[/yellow] {env_var} usually starts with [bold]{secret.warning_prefix}[/bold]."
        )
    if source != "prompt":
        console.print(f"  [dim]using {env_var} from {source}[/dim]")
    return ResolvedSecret(env_var=env_var, value=value)


def _ensure_setup_identity(
    *,
    username: str,
    transport: SetupTransport,
    external_id: str,
) -> str:
    store = get_store()
    user = store.get_user(username)
    platform_id = f"{transport.name}:{external_id}"
    existing_username = store.resolve_username(platform_id)

    if existing_username and existing_username != username:
        console.print(
            f"[red]Error:[/red] identity '{platform_id}' is already linked to '{existing_username}'."
        )
        raise typer.Exit(code=1)

    messages: list[str] = []
    if user is None:
        store.add_user(username)
        messages.append(f"created user '{username}'")
    else:
        messages.append(f"using existing user '{username}'")

    if "admin" not in store.get_user_roles(username):
        store.add_role(username, "admin")
        messages.append("granted admin role")
    else:
        messages.append("admin role already present")

    if existing_username == username:
        messages.append(f"identity '{platform_id}' already linked")
    else:
        store.add_identity(username, platform_id)
        messages.append(f"linked '{platform_id}'")

    return ", ".join(messages)


@app.command("init")
def init() -> None:
    """Scaffold the ~/.operator directory with starter config."""
    result = _scaffold_operator_home(
        OPERATOR_DIR,
        config_text=_build_starter_config(),
    )
    if not result.wrote_config:
        console.print(f"\n[bold]{result.config_file}[/bold] already exists.")
    console.print(f"\n[bold green]Operator initialized at {result.home}[/bold green]")
    console.print("Next step: [bold]operator setup[/bold] for the guided onboarding flow.")
    console.print(
        "Manual path: [bold]operator user add <username> --role admin <transport> <id>[/bold]"
    )


@app.command("setup")
def setup(
    username: str | None = typer.Option(None, "--username", help="Admin username to create."),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Model provider to configure: anthropic, openai, or gemini.",
    ),
    timezone: str | None = typer.Option(
        None,
        "--timezone",
        help="IANA timezone to write into operator.yaml, for example America/Vancouver.",
    ),
    transport: str | None = typer.Option(None, "--transport", help="Transport to configure."),
    identity: str | None = typer.Option(
        None,
        "--identity",
        help="Transport identity for your admin user.",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Provider API key to persist into ~/.operator/.env."
    ),
    secret: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--secret",
        help="Transport secret override as ENV_VAR=value. Repeat for multiple values.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-prompt for secrets even if they already exist."
    ),
    run: bool = typer.Option(
        False, "--run/--no-run", help="Start operator in the foreground after setup."
    ),
) -> None:
    """Guided onboarding from a fresh install to the first transport-backed agent."""
    selected_provider = _resolve_setup_provider(provider)
    selected_transport = _resolve_setup_transport(transport)

    console.print("[bold]Operator setup[/bold]")
    console.print(
        f"This will save the minimum config for your first {selected_transport.label}-backed agent and admin user.\n"
    )
    console.print(
        f"Using transport: [bold]{selected_transport.label}[/bold] "
        f"([dim]{selected_transport.description}[/dim])"
    )
    console.print(f"Provider: [bold]{selected_provider.label}[/bold]")
    console.print(f"Default agent: [bold]{_DEFAULT_AGENT_NAME}[/bold]")
    console.print(f"Default model: [bold]{selected_provider.default_model}[/bold]")

    selected_timezone = _prompt_timezone(timezone)
    console.print(f"Timezone: [bold]{selected_timezone}[/bold]\n")
    resolved_username = _prompt_username(username)

    result = _scaffold_operator_home(
        OPERATOR_DIR,
        config_text=_build_starter_config(
            default_model=selected_provider.default_model,
            transport_name=selected_transport.name,
            transport_env=selected_transport.env_defaults,
            transport_settings=selected_transport.settings_defaults,
        ),
        emit_output=False,
    )
    env_file_values = parse_env_file(result.env_file)

    secrets: dict[str, str] = {}
    provider_secret = _resolve_secret(
        cli_value=api_key,
        secret=selected_provider.secret,
        env_file_values=env_file_values,
        env_file=result.env_file,
        force=force,
    )
    secrets[provider_secret.env_var] = provider_secret.value

    transport_secret_overrides = _parse_secret_overrides(secret)
    allowed_secret_envs = {
        transport_secret.env_var for transport_secret in selected_transport.secrets
    }
    unknown_secret_envs = sorted(set(transport_secret_overrides) - allowed_secret_envs)
    if unknown_secret_envs:
        raise typer.BadParameter(
            f"Unknown secret override(s) for transport {selected_transport.name!r}: {', '.join(unknown_secret_envs)}",
            param_hint="--secret",
        )
    for transport_secret in selected_transport.secrets:
        resolved_secret = _resolve_secret(
            cli_value=transport_secret_overrides.get(transport_secret.env_var),
            secret=transport_secret,
            env_file_values=env_file_values,
            env_file=result.env_file,
            force=force,
        )
        secrets[resolved_secret.env_var] = resolved_secret.value

    if identity is not None:
        try:
            external_id = selected_transport.normalize_identity(identity)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--identity") from None
    else:
        external_id = ""
    while not external_id:
        console.print(f"[dim]{selected_transport.identity_help}[/dim]")
        transport_identity = typer.prompt(selected_transport.identity_prompt).strip()
        try:
            external_id = selected_transport.normalize_identity(transport_identity)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")

    _update_env_file(result.env_file, secrets)
    user_status = _ensure_setup_identity(
        username=resolved_username,
        transport=selected_transport,
        external_id=external_id,
    )

    # Set timezone on the user record
    store = get_store()
    store.set_user_timezone(resolved_username, selected_timezone)

    console.print(f"\n[green]Updated[/green] {result.env_file}")
    if result.wrote_config:
        console.print(f"[green]Created[/green] {result.config_file}")
    else:
        console.print(f"[dim]Using existing config[/dim] {result.config_file}")
    console.print(f"[green]Ready[/green] {user_status}")

    if run:
        console.print(f"\nStarting operator now. {selected_transport.run_hint}")
        asyncio.run(async_main())
        return

    console.print("\nNext:")
    for index, step in enumerate(selected_transport.next_steps, start=1):
        console.print(f"  {index}. {step}")


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
