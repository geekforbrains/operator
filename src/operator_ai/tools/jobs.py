from __future__ import annotations

import shutil
import stat
from pathlib import Path

from croniter import croniter

from operator_ai.config import load_config
from operator_ai.jobs import JOBS_DIR, scan_jobs
from operator_ai.skills import parse_frontmatter, rewrite_frontmatter
from operator_ai.store import get_store
from operator_ai.tools.registry import tool


def _safe_job_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid job name: {name!r}")
    return name


def _safe_job_relative_path(job_dir: Path, path: str) -> Path:
    if not path:
        raise ValueError("hook script path cannot be empty")
    p = Path(path)
    if p.is_absolute():
        raise ValueError(f"hook script path must be relative: {path!r}")
    resolved = (job_dir / p).resolve()
    try:
        resolved.relative_to(job_dir.resolve())
    except ValueError as e:
        raise ValueError(f"hook script path escapes job directory: {path!r}") from e
    return resolved


@tool(
    description="Manage scheduled jobs. Actions: list, create, update, delete, enable, disable.",
)
async def manage_job(action: str, name: str = "", config: str = "") -> str:
    """Manage scheduled jobs.

    Args:
        action: One of: list, create, update, delete, enable, disable.
        name: Job directory name (required for all actions except list).
        config: Full JOB.md content for create/update. YAML frontmatter (between --- delimiters) with fields: name, description, schedule (cron, required), agent (optional — agent name to run as), model (optional, litellm format), enabled, hooks (prerun/postrun scripts). Body is the prompt — include posting instructions (channels, threading) since the agent uses send_message to deliver output.
    """
    action = action.lower().strip()

    if action == "list":
        return _list_jobs()
    elif action == "create":
        return _create_job(name, config)
    elif action == "update":
        return _update_job(name, config)
    elif action == "delete":
        return _delete_job(name)
    elif action == "enable":
        return _toggle_job(name, enabled=True)
    elif action == "disable":
        return _toggle_job(name, enabled=False)
    else:
        return (
            f"[error: unknown action '{action}'. "
            "Use: list, create, update, delete, enable, disable]"
        )


def _list_jobs() -> str:
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
            f"  last_run: {state.last_run or 'never'} ({state.last_result or '-'})\n"
            f"  runs: {state.run_count}, skips: {state.skip_count}"
        )

    return "\n".join(lines)


def _validate_frontmatter(fm: dict) -> str | None:
    config = load_config()
    agent_names = list(config.agents.keys())

    if not fm.get("schedule"):
        return "[error: frontmatter must include a 'schedule' field]"

    if not croniter.is_valid(fm["schedule"]):
        return f"[error: invalid cron schedule: {fm['schedule']}]"

    agent = fm.get("agent")
    if agent and agent not in agent_names:
        return f"[error: unknown agent '{agent}'. Available agents: {', '.join(agent_names)}]"

    hooks = fm.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return "[error: 'hooks' must be a mapping (e.g. hooks:\\n  prerun: scripts/check.sh)]"
    if isinstance(hooks, dict):
        job_dir = JOBS_DIR / "__validation__"
        for hook_name, script_path in hooks.items():
            if not isinstance(script_path, str):
                return f"[error: hooks.{hook_name} must be a string path]"
            try:
                _safe_job_relative_path(job_dir, script_path)
            except ValueError as e:
                return f"[error: {e}]"

    return None


def _create_job(name: str, config: str) -> str:
    if not name:
        return "[error: 'name' is required for create]"
    if not config:
        return "[error: 'config' (JOB.md content) is required for create]"

    job_dir = JOBS_DIR / _safe_job_name(name)
    if job_dir.exists():
        return f"[error: job '{name}' already exists. Use 'update' to modify.]"

    fm = parse_frontmatter(config)
    if not fm:
        return "[error: config must have YAML frontmatter between --- delimiters]"

    err = _validate_frontmatter(fm)
    if err:
        return err

    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "JOB.md").write_text(config)

    # Create placeholder hook scripts if referenced
    hooks = fm.get("hooks") or {}
    if isinstance(hooks, dict):
        for hook_name, script_path in hooks.items():
            try:
                full_path = _safe_job_relative_path(job_dir, str(script_path))
            except ValueError as e:
                return f"[error: {e}]"
            full_path.parent.mkdir(parents=True, exist_ok=True)
            if not full_path.exists():
                full_path.write_text(f"#!/bin/bash\n# {hook_name} hook for {name}\nexit 0\n")
                full_path.chmod(full_path.stat().st_mode | stat.S_IEXEC)

    return f"Created job '{name}' at {job_dir}"


def _update_job(name: str, config: str) -> str:
    if not name:
        return "[error: 'name' is required for update]"
    if not config:
        return "[error: 'config' (JOB.md content) is required for update]"

    job_dir = JOBS_DIR / _safe_job_name(name)
    if not job_dir.exists():
        return f"[error: job '{name}' not found]"

    fm = parse_frontmatter(config)
    if not fm:
        return "[error: config must have YAML frontmatter between --- delimiters]"

    err = _validate_frontmatter(fm)
    if err:
        return err

    (job_dir / "JOB.md").write_text(config)
    return f"Updated job '{name}'"


def _delete_job(name: str) -> str:
    if not name:
        return "[error: 'name' is required for delete]"

    job_dir = JOBS_DIR / _safe_job_name(name)
    if not job_dir.exists():
        return f"[error: job '{name}' not found]"

    shutil.rmtree(job_dir)
    return f"Deleted job '{name}'"


def _toggle_job(name: str, *, enabled: bool) -> str:
    if not name:
        return f"[error: 'name' is required for {'enable' if enabled else 'disable'}]"

    job_dir = JOBS_DIR / _safe_job_name(name)
    job_md = job_dir / "JOB.md"
    if not job_md.exists():
        return f"[error: job '{name}' not found]"

    if not rewrite_frontmatter(job_md, {"enabled": enabled}):
        return f"[error: could not parse frontmatter for '{name}']"

    action = "Enabled" if enabled else "Disabled"
    return f"{action} job '{name}'"
