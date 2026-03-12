"""Deterministic job management tools.

Each tool has explicit typed parameters so the agent never composes raw
YAML frontmatter.  The tools assemble the job file internally.
"""

from __future__ import annotations

import stat
from pathlib import Path

import yaml
from croniter import croniter

from operator_ai.config import OPERATOR_DIR, ConfigError, load_config
from operator_ai.job_specs import JOBS_DIR
from operator_ai.jobs import scan_jobs
from operator_ai.message_timestamps import format_ts
from operator_ai.skills import rewrite_frontmatter
from operator_ai.store import get_store
from operator_ai.tools.registry import safe_name, tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_hook_relative_path(base_dir: Path, path: str) -> Path:
    """Validate a hook script path is relative and stays within base_dir."""
    if not path:
        raise ValueError("hook script path cannot be empty")
    p = Path(path)
    if p.is_absolute():
        raise ValueError(f"hook script path must be relative: {path!r}")
    resolved = (base_dir / p).resolve()
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError as e:
        raise ValueError(f"hook script path escapes operator directory: {path!r}") from e
    return resolved


def _validate_agent(agent: str) -> str | None:
    """Return an error string if the agent name is invalid, else None."""
    if not agent:
        return None
    try:
        cfg = load_config()
    except ConfigError:
        return "[error: unable to load config]"
    if agent not in cfg.agents:
        names = ", ".join(cfg.agents.keys())
        return f"[error: unknown agent '{agent}'. Available: {names}]"
    return None


def _validate_hook(path: str, label: str) -> str | None:
    """Return an error string if a hook path is invalid, else None."""
    if not path:
        return None
    try:
        _safe_hook_relative_path(OPERATOR_DIR, path)
    except ValueError as e:
        return f"[error: {label}: {e}]"
    return None


def _ensure_hook_script(path: str, hook_name: str, job_name: str) -> str | None:
    """Create a placeholder hook script if it doesn't exist. Returns error or None."""
    if not path:
        return None
    try:
        full_path = _safe_hook_relative_path(OPERATOR_DIR, path)
    except ValueError as e:
        return f"[error: {e}]"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    if not full_path.exists():
        full_path.write_text(f"#!/bin/bash\n# {hook_name} hook for {job_name}\nexit 0\n")
        full_path.chmod(full_path.stat().st_mode | stat.S_IEXEC)
    return None


def _build_job_file(
    *,
    name: str,
    schedule: str,
    prompt: str,
    description: str = "",
    agent: str = "",
    model: str = "",
    max_iterations: int = 0,
    enabled: bool = True,
    prerun: str = "",
    postrun: str = "",
) -> str:
    """Assemble a job .md file from structured fields."""
    fm: dict = {"name": name, "schedule": schedule}
    if description:
        fm["description"] = description
    if agent:
        fm["agent"] = agent
    if model:
        fm["model"] = model
    if max_iterations:
        fm["max_iterations"] = max_iterations
    fm["enabled"] = enabled
    hooks: dict[str, str] = {}
    if prerun:
        hooks["prerun"] = prerun
    if postrun:
        hooks["postrun"] = postrun
    if hooks:
        fm["hooks"] = hooks
    fm_text = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{fm_text}\n---\n\n{prompt}\n"


def _job_file(name: str) -> tuple[Path, str | None]:
    """Resolve job file path, returning (path, error_or_none)."""
    try:
        slug = safe_name(name, "job")
    except ValueError as e:
        return JOBS_DIR / "invalid", f"[error: {e}]"
    return JOBS_DIR / f"{slug}.md", None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    description=(
        "Create a new scheduled job. The tool assembles the job file — just provide the fields."
    ),
)
async def create_job(
    name: str,
    schedule: str,
    prompt: str,
    description: str = "",
    agent: str = "",
    model: str = "",
    max_iterations: int = 0,
    enabled: bool = True,
    prerun: str = "",
    postrun: str = "",
) -> str:
    """Create a scheduled job.

    Args:
        name: Job slug (lowercase, hyphens, no spaces).
        schedule: Cron expression (e.g. "0 8 * * *" for daily at 8 AM).
        prompt: The prompt body the agent receives each run. Use send_message in the prompt to deliver output. If using a prerun script, reference its output to avoid redundant LLM work.
        description: Short human-readable summary of what the job does.
        agent: Agent to run as (omit for the default agent).
        model: Model override in litellm format (omit for agent default).
        max_iterations: Override max tool-call iterations (0 = agent default). Increase for complex multi-step jobs.
        enabled: Whether the job is active (default true).
        prerun: Relative path to a prerun gate script. Non-zero exit skips the run. Stdout is injected into the prompt as context — use this to pre-filter data so the model works on concrete input, not raw fetching.
        postrun: Relative path to a postrun script. Receives agent output on stdin. Non-zero exit marks the run failed.
    """
    if not name:
        return "[error: name is required]"
    if not schedule:
        return "[error: schedule is required]"
    if not prompt:
        return "[error: prompt is required]"

    if not croniter.is_valid(schedule):
        return f"[error: invalid cron schedule: {schedule!r}]"

    job_file, err = _job_file(name)
    if err:
        return err
    if job_file.exists():
        return f"[error: job '{name}' already exists. Use update_job to modify.]"

    err = _validate_agent(agent)
    if err:
        return err
    err = _validate_hook(prerun, "prerun")
    if err:
        return err
    err = _validate_hook(postrun, "postrun")
    if err:
        return err

    content = _build_job_file(
        name=name,
        schedule=schedule,
        prompt=prompt,
        description=description,
        agent=agent,
        model=model,
        max_iterations=max_iterations,
        enabled=enabled,
        prerun=prerun,
        postrun=postrun,
    )

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_file.write_text(content)

    # Create placeholder hook scripts
    err = _ensure_hook_script(prerun, "prerun", name)
    if err:
        return err
    err = _ensure_hook_script(postrun, "postrun", name)
    if err:
        return err

    return f"Created job '{name}' at {job_file}"


@tool(
    description=(
        "Update an existing scheduled job. Replaces the job file with new values. "
        "All fields are re-specified to keep the file consistent."
    ),
)
async def update_job(
    name: str,
    schedule: str,
    prompt: str,
    description: str = "",
    agent: str = "",
    model: str = "",
    max_iterations: int = 0,
    enabled: bool = True,
    prerun: str = "",
    postrun: str = "",
) -> str:
    """Update a scheduled job (full replace).

    Args:
        name: Job slug to update.
        schedule: Cron expression.
        prompt: The prompt body.
        description: Short human-readable summary.
        agent: Agent to run as (omit for default).
        model: Model override in litellm format.
        max_iterations: Override max iterations (0 = agent default).
        enabled: Whether the job is active.
        prerun: Relative path to prerun gate script.
        postrun: Relative path to postrun script.
    """
    if not name:
        return "[error: name is required]"
    if not schedule:
        return "[error: schedule is required]"
    if not prompt:
        return "[error: prompt is required]"

    if not croniter.is_valid(schedule):
        return f"[error: invalid cron schedule: {schedule!r}]"

    job_file, err = _job_file(name)
    if err:
        return err
    if not job_file.exists():
        return f"[error: job '{name}' not found]"

    err = _validate_agent(agent)
    if err:
        return err
    err = _validate_hook(prerun, "prerun")
    if err:
        return err
    err = _validate_hook(postrun, "postrun")
    if err:
        return err

    content = _build_job_file(
        name=name,
        schedule=schedule,
        prompt=prompt,
        description=description,
        agent=agent,
        model=model,
        max_iterations=max_iterations,
        enabled=enabled,
        prerun=prerun,
        postrun=postrun,
    )

    job_file.write_text(content)

    # Create placeholder hook scripts if new hooks were added
    err = _ensure_hook_script(prerun, "prerun", name)
    if err:
        return err
    err = _ensure_hook_script(postrun, "postrun", name)
    if err:
        return err

    return f"Updated job '{name}'"


@tool(description="Delete a scheduled job.")
async def delete_job(name: str) -> str:
    """Delete a job.

    Args:
        name: Job slug to delete.
    """
    if not name:
        return "[error: name is required]"

    job_file, err = _job_file(name)
    if err:
        return err
    if not job_file.exists():
        return f"[error: job '{name}' not found]"

    job_file.unlink()
    return f"Deleted job '{name}'"


@tool(description="Enable a scheduled job.")
async def enable_job(name: str) -> str:
    """Enable a job.

    Args:
        name: Job slug to enable.
    """
    if not name:
        return "[error: name is required]"

    job_file, err = _job_file(name)
    if err:
        return err
    if not job_file.exists():
        return f"[error: job '{name}' not found]"

    if not rewrite_frontmatter(job_file, {"enabled": True}):
        return f"[error: could not parse frontmatter for '{name}']"
    return f"Enabled job '{name}'"


@tool(description="Disable a scheduled job without deleting it.")
async def disable_job(name: str) -> str:
    """Disable a job.

    Args:
        name: Job slug to disable.
    """
    if not name:
        return "[error: name is required]"

    job_file, err = _job_file(name)
    if err:
        return err
    if not job_file.exists():
        return f"[error: job '{name}' not found]"

    if not rewrite_frontmatter(job_file, {"enabled": False}):
        return f"[error: could not parse frontmatter for '{name}']"
    return f"Disabled job '{name}'"


@tool(description="List all scheduled jobs with their status and last run info.")
async def list_jobs() -> str:
    """List jobs."""
    jobs = scan_jobs()
    if not jobs:
        return "No jobs found."

    lines: list[str] = []
    for job in jobs:
        state = get_store().load_job_state(job.name)
        status = "enabled" if job.enabled else "disabled"

        lines.append(
            f"- **{job.name}** ({status})\n"
            f"  schedule: `{job.schedule}`\n"
            f"  description: {job.description}\n"
            f"  agent: {job.agent or '(default)'}\n"
            f"  model: {job.model or '(agent default)'}\n"
            f"  last_run: {format_ts(state.last_run) if state.last_run else 'never'}"
            f" ({state.last_result or '-'})\n"
            f"  runs: {state.run_count}, skips: {state.skip_count}"
        )

    return "\n".join(lines)
