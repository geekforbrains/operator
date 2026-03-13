from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from operator_ai.config import LOGS_DIR, OPERATOR_DIR, ConfigError, load_config
from operator_ai.frontmatter import rewrite_frontmatter
from operator_ai.job import find_job, run_job_now, scan_jobs
from operator_ai.log_context import setup_logging
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import format_ts
from operator_ai.store import get_store
from operator_ai.transport.cli import CliTransport
from operator_ai.transport.registry import transport_logger_names

console = Console()

job_app = typer.Typer(help="Job inspection and management.")


def _setup_cli_logging() -> None:
    """Set up logging for CLI commands — writes to the shared log file + stderr."""
    setup_logging(
        log_dir=LOGS_DIR,
        stderr=True,
        noisy_loggers=("httpx", "httpcore", "litellm", "openai", *transport_logger_names()),
    )


def _scan_jobs():
    return scan_jobs(OPERATOR_DIR / "jobs")


def _find_job(name: str):
    return find_job(name, OPERATOR_DIR / "jobs")


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
