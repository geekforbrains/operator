"""Deterministic job management tools.

Each tool has explicit typed parameters so the agent never composes raw
YAML frontmatter.  The tools assemble the job file internally.

Jobs live at ``jobs/<name>/JOB.md``. Hook scripts use explicit relative
paths, with ``scripts/prerun.sh`` and ``scripts/postrun.sh`` as the
recommended convention.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import yaml
from croniter import croniter

from operator_ai.config import ConfigError, load_config
from operator_ai.frontmatter import rewrite_frontmatter
from operator_ai.job.spec import scan_jobs
from operator_ai.message_timestamps import format_ts
from operator_ai.store import get_store
from operator_ai.tools.context import resolve_dir
from operator_ai.tools.registry import safe_name, tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_PRERUN = "scripts/prerun.sh"
DEFAULT_POSTRUN = "scripts/postrun.sh"


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


def _normalize_hook_path(path: str) -> str:
    return path.strip()


def _validate_hook_path(job_dir: Path, path: str, label: str) -> str | None:
    """Return an error string if a hook path is invalid for this job."""
    if not path:
        return None
    p = Path(path)
    if p.is_absolute():
        return f"[error: {label} must be a relative path: {path!r}]"
    if not p.name:
        return f"[error: {label} must point to a runnable script file: {path!r}]"
    resolved = (job_dir / p).resolve()
    try:
        resolved.relative_to(job_dir.resolve())
    except ValueError:
        return f"[error: {label} path escapes job directory: {path!r}]"
    return None


def _ensure_hook_script(job_dir: Path, path: str, hook_name: str, job_name: str) -> str | None:
    """Create a placeholder hook script at the configured path if it doesn't exist."""
    if not path:
        return None
    full_path = job_dir / path
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
    """Assemble a JOB.md file from structured fields."""
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


def _jobs_dir() -> Path:
    return resolve_dir("jobs")


def _job_dir(name: str) -> tuple[Path, str | None]:
    """Resolve job directory path, returning (path, error_or_none)."""
    jobs_dir = _jobs_dir()
    try:
        slug = safe_name(name, "job")
    except ValueError as e:
        return jobs_dir / "invalid", f"[error: {e}]"
    return jobs_dir / slug, None


def _validate_job_inputs(name: str, schedule: str, prompt: str, agent: str) -> str | None:
    """Shared validation for create/update. Returns error string or None."""
    if not name:
        return "[error: name is required]"
    if not schedule:
        return "[error: schedule is required]"
    if not prompt:
        return "[error: prompt is required]"
    if not croniter.is_valid(schedule):
        return f"[error: invalid cron schedule: {schedule!r}]"
    return _validate_agent(agent)


def _write_job_file(
    job_dir: Path,
    *,
    name: str,
    schedule: str,
    prompt: str,
    description: str,
    agent: str,
    model: str,
    max_iterations: int,
    enabled: bool,
    prerun: str,
    postrun: str,
) -> None:
    """Build and write JOB.md plus placeholder hook scripts."""
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

    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "JOB.md").write_text(content)
    _ensure_hook_script(job_dir, prerun, "prerun", name)
    _ensure_hook_script(job_dir, postrun, "postrun", name)


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
        prerun: Relative path to a runnable prerun script inside the job directory. Leave empty to disable. Recommended convention: scripts/prerun.sh. Stdout is injected into the prompt as context.
        postrun: Relative path to a runnable postrun script inside the job directory. Leave empty to disable. Recommended convention: scripts/postrun.sh. Receives agent output on stdin.
    """
    err = _validate_job_inputs(name, schedule, prompt, agent)
    if err:
        return err

    job_dir, err = _job_dir(name)
    if err:
        return err
    if job_dir.exists():
        return f"[error: job '{name}' already exists. Use update_job to modify.]"

    prerun = _normalize_hook_path(prerun)
    postrun = _normalize_hook_path(postrun)
    if err := _validate_hook_path(job_dir, prerun, "prerun"):
        return err
    if err := _validate_hook_path(job_dir, postrun, "postrun"):
        return err

    _write_job_file(
        job_dir,
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
    return f"Created job '{name}' at {job_dir}"


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
        prerun: Relative path to a runnable prerun script inside the job directory. Leave empty to disable. Recommended convention: scripts/prerun.sh.
        postrun: Relative path to a runnable postrun script inside the job directory. Leave empty to disable. Recommended convention: scripts/postrun.sh.
    """
    err = _validate_job_inputs(name, schedule, prompt, agent)
    if err:
        return err

    job_dir, err = _job_dir(name)
    if err:
        return err
    if not job_dir.exists():
        return f"[error: job '{name}' not found]"

    prerun = _normalize_hook_path(prerun)
    postrun = _normalize_hook_path(postrun)
    if err := _validate_hook_path(job_dir, prerun, "prerun"):
        return err
    if err := _validate_hook_path(job_dir, postrun, "postrun"):
        return err

    _write_job_file(
        job_dir,
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
    return f"Updated job '{name}'"


@tool(description="Delete a scheduled job and its entire directory.")
async def delete_job(name: str) -> str:
    """Delete a job.

    Args:
        name: Job slug to delete.
    """
    if not name:
        return "[error: name is required]"

    job_dir, err = _job_dir(name)
    if err:
        return err
    if not job_dir.exists():
        return f"[error: job '{name}' not found]"

    shutil.rmtree(job_dir)
    return f"Deleted job '{name}'"


@tool(description="Enable a scheduled job.")
async def enable_job(name: str) -> str:
    """Enable a job.

    Args:
        name: Job slug to enable.
    """
    if not name:
        return "[error: name is required]"

    job_dir, err = _job_dir(name)
    if err:
        return err
    job_md = job_dir / "JOB.md"
    if not job_md.exists():
        return f"[error: job '{name}' not found]"

    if not rewrite_frontmatter(job_md, {"enabled": True}):
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

    job_dir, err = _job_dir(name)
    if err:
        return err
    job_md = job_dir / "JOB.md"
    if not job_md.exists():
        return f"[error: job '{name}' not found]"

    if not rewrite_frontmatter(job_md, {"enabled": False}):
        return f"[error: could not parse frontmatter for '{name}']"
    return f"Disabled job '{name}'"


@tool(description="List all scheduled jobs with their status and last run info.")
async def list_jobs() -> str:
    """List jobs."""
    jobs = scan_jobs(_jobs_dir())
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
