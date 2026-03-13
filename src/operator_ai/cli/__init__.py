from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import operator_ai.tools  # noqa: F401
from operator_ai.agent import load_configured_agents
from operator_ai.cli.common import cli_base_dir, load_cli_config
from operator_ai.cli.init import init_cmd
from operator_ai.cli.jobs import job_app
from operator_ai.cli.memory import memory_app
from operator_ai.cli.service import service_app
from operator_ai.cli.users import user_app
from operator_ai.config import ConfigError, load_config
from operator_ai.main import async_main
from operator_ai.skills import scan_skills
from operator_ai.tools.registry import get_tools

console = Console()

app = typer.Typer(add_completion=False)
skill_app = typer.Typer(help="Manage skills.")

app.add_typer(job_app, name="job")
app.add_typer(service_app, name="service")
app.add_typer(memory_app, name="memory")
app.add_typer(skill_app, name="skills")
app.add_typer(user_app, name="user")

app.command("init")(init_cmd)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Operator - local AI agent runtime."""
    if ctx.invoked_subcommand is None:
        asyncio.run(async_main())


# ── Logs ─────────────────────────────────────────────────────


@app.command("logs")
def logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output."),
    lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show."),
) -> None:
    """Tail the operator log file."""
    log_file = cli_base_dir(load_cli_config()) / "logs" / "operator.log"
    if not log_file.exists():
        print(f"No log file found at {log_file}")
        raise typer.Exit(code=1)
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(log_file))
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(cmd)


# ── Config ───────────────────────────────────────────────────


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


# ── Agents ───────────────────────────────────────────────────


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

    agent_infos = {a.name: a for a in load_configured_agents(config)}

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


# ── Skills ───────────────────────────────────────────────────


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
    skills_dir = cli_base_dir(load_cli_config()) / "skills"
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


# ── Tools ────────────────────────────────────────────────────


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
