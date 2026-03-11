from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import operator_ai.tools  # noqa: F401
from operator_ai.agents import scan_agents
from operator_ai.config import OPERATOR_DIR, ConfigError, load_config
from operator_ai.job_specs import find_job_spec, scan_job_specs
from operator_ai.jobs import run_job_now
from operator_ai.log_context import RunContextFilter
from operator_ai.main import async_main
from operator_ai.memory import MemoryStore
from operator_ai.prompts import load_prompt
from operator_ai.skills import (
    install_bundled_skills,
    list_bundled_skill_names,
    reset_bundled_skill,
    rewrite_frontmatter,
    scan_skills,
)
from operator_ai.store import get_store
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

    for name in ("httpx", "httpcore", "litellm", "openai", *transport_logger_names()):
        logging.getLogger(name).setLevel(logging.WARNING)


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


def _store():
    return get_store()


def _is_macos() -> bool:
    return sys.platform == "darwin"


# ── Init command ──────────────────────────────────────────────

_DEFAULT_AGENT_NAME = "operator"
_DEFAULT_PROVIDER = "anthropic"
_USERNAME_RE = re.compile(r"^[a-z0-9.\-]{1,64}$")


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
    timezone: str = "UTC",
    transport_name: str | None = None,
    transport_options: dict[str, object] | None = None,
) -> str:
    selected_transport = default_setup_transport()
    resolved_transport_name = transport_name or selected_transport.name
    resolved_transport_options = transport_options or dict(selected_transport.config_defaults)
    transport_lines = [f"type: {resolved_transport_name}"]
    for key, value in resolved_transport_options.items():
        rendered = json.dumps(value) if isinstance(value, str) else str(value).lower()
        transport_lines.append(f"{key}: {rendered}")
    transport_block = "\n".join(f"          {line}" for line in transport_lines)

    return textwrap.dedent(f"""\
        # Operator configuration
        # Docs: https://operator.geekforbrains.com
        # Repo: https://github.com/geekforbrains/operator

        runtime:
          timezone: "{timezone}"
          env_file: ".env"
          show_usage: false
          # How an agent responds when messaged from an unknown user.
          # - announce: responds with a simple message
          # - ignore: does not respond at all
          reject_response: ignore

        defaults:
          # Model fallback chain
          # first model is preferred, rest are fallbacks.
          # Uses LiteLLM format: provider/model-name
          models:
            - "{default_model}"
            # - "some-provider/some-other-model"
          max_iterations: 50
          context_ratio: 0.5

        agents:
          {_DEFAULT_AGENT_NAME}:
            transport:
{transport_block}

        roles:
          guest:
            agents: []

        # memory:
        #   embed_model: "openai/text-embedding-3-small"
        #   embed_dimensions: 1536
        #   inject_top_k: 3
        #   inject_min_relevance: 0.3
        #   candidate_ttl_days: 14
        #   harvester:
        #     enabled: true
        #     schedule: "*/30 * * * *"
        #     model: "openai/gpt-4.1-mini"
        #   cleaner:
        #     enabled: true
        #     schedule: "0 3 * * *"
        #     model: "openai/gpt-4.1-mini"
        """)


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
        home / "state",
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
        (home / "agents" / _DEFAULT_AGENT_NAME / "AGENT.md", load_prompt("agent.md")),
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

    installed = install_bundled_skills(home / "skills")
    if emit_output:
        for skill_name in installed:
            console.print(f"  [green]skill[/green]  {skill_name}")

    return ScaffoldResult(
        home=home,
        config_file=config_file,
        env_file=env_file,
        wrote_config=wrote_config,
        wrote_env_file=wrote_env_file,
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


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
        if not _USERNAME_RE.match(value):
            raise typer.BadParameter(
                "Username must be 1-64 chars using lowercase letters, numbers, dots, and hyphens."
            )
        return value

    default = _default_setup_username()
    while True:
        value = typer.prompt("Admin username", default=default).strip()
        if _USERNAME_RE.match(value):
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
    store = _store()
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
        config_text=_build_starter_config(timezone=_detect_local_timezone()),
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
            timezone=selected_timezone,
            transport_name=selected_transport.name,
            transport_options=selected_transport.config_defaults,
        ),
        emit_output=False,
    )
    env_file_values = _parse_env_file(result.env_file)

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
            <string>{LOG_DIR / "operator.log"}</string>
            <key>StandardErrorPath</key>
            <string>{LOG_DIR / "operator.log"}</string>
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
    _setup_cli_logging()
    cli_logger = logging.getLogger("operator.cli")

    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    store = (
        get_store(embed_dimensions=config.memory.embed_dimensions)
        if config.memory.enabled
        else get_store()
    )
    memory_store = MemoryStore(store, config.memory) if config.memory.enabled else None

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
    table.add_column("Retention", style="magenta")
    table.add_column("Expires", style="dim")
    table.add_column("Content")
    table.add_column("", width=1)  # pin marker
    for row in rows:
        content = row["content"].replace("\n", " ")
        if len(content) > 100:
            content = content[:97] + "..."
        pin = Text("\u2691", style="yellow") if row["pinned"] else Text("")
        expires = row["expires_at"] or ""
        table.add_row(
            str(row["id"]),
            Text(f"{row['scope']}/{row['scope_id']}", style="cyan"),
            row["retention"],
            expires,
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
    table.add_column("Candidate", justify="right", style="magenta")
    table.add_column("Durable", justify="right", style="green")
    table.add_column("Pinned", justify="right", style="yellow")

    total = 0
    total_candidate = 0
    total_durable = 0
    total_pinned = 0
    for row in rows:
        count = row["count"]
        candidate_count = row["candidate_count"] or 0
        durable_count = row["durable_count"] or 0
        pinned_count = row["pinned"] or 0
        total += count
        total_candidate += candidate_count
        total_durable += durable_count
        total_pinned += pinned_count
        table.add_row(
            f"{row['scope']}/{row['scope_id']}",
            str(count),
            str(candidate_count) if candidate_count else "",
            str(durable_count) if durable_count else "",
            str(pinned_count) if pinned_count else "",
        )
    table.add_section()
    table.add_row(
        Text("Total", style="bold"),
        Text(str(total), style="bold"),
        str(total_candidate),
        str(total_durable),
        str(total_pinned),
    )
    console.print(table)


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


@skill_app.command("reset")
def skills_reset(
    name: str = typer.Argument(None, help="Bundled skill name to reset."),
    all_skills: bool = typer.Option(False, "--all", help="Reset all bundled skills."),
) -> None:
    """Reset bundled skill(s) to their original version."""
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
            config = load_config()
            if role not in config.roles:
                console.print(f"[yellow]Warning:[/yellow] role '{role}' is not defined in config.")
        except ConfigError:
            pass

    store = _store()
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
    store = _store()
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
    store = _store()
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
