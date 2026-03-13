"""Job definitions — scan, parse, and find jobs on disk.

Jobs live at ``jobs/<name>/JOB.md`` with YAML frontmatter for metadata
and a markdown body as the prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from croniter import croniter

from operator_ai.config import OPERATOR_DIR
from operator_ai.frontmatter import extract_body, parse_frontmatter

logger = logging.getLogger("operator.job")

JOBS_DIR = OPERATOR_DIR / "jobs"


@dataclass
class Job:
    name: str
    description: str
    schedule: str
    prompt: str
    path: Path  # absolute path to JOB.md
    agent: str = ""
    model: str = ""
    max_iterations: int = 0
    hooks: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def scan_jobs(jobs_dir: Path = JOBS_DIR) -> list[Job]:
    """Scan ``jobs/<name>/JOB.md`` and return fully-parsed Job objects."""
    if not jobs_dir.is_dir():
        return []

    jobs: list[Job] = []
    for child in sorted(jobs_dir.iterdir()):
        if not child.is_dir():
            continue
        job_md = child / "JOB.md"
        if not job_md.exists():
            continue
        job = _read_job(job_md)
        if job is not None:
            jobs.append(job)
    return jobs


def find_job(name: str, jobs_dir: Path = JOBS_DIR) -> Job | None:
    """Find a single job by name.

    Fast path: try ``jobs/<name>/JOB.md`` directly.
    Slow path: scan all jobs if the fast path misses (frontmatter name differs from dir name).
    """
    job_md = jobs_dir / name / "JOB.md"
    if job_md.is_file():
        job = _read_job(job_md)
        if job is not None and job.name == name:
            return job

    for job in scan_jobs(jobs_dir):
        if job.name == name:
            return job
    return None


def _read_job(job_md: Path) -> Job | None:
    """Parse a single JOB.md into a Job, or None on failure."""
    try:
        text = job_md.read_text()
        fm = parse_frontmatter(text)
    except Exception:
        logger.warning("Failed to parse job frontmatter in %s", job_md)
        return None
    if not fm:
        return None

    schedule = fm.get("schedule", "")
    if not schedule or not croniter.is_valid(schedule):
        if schedule:
            logger.warning("Invalid schedule '%s' in %s, skipping", schedule, job_md)
        return None

    # Coerce hooks to dict (agents sometimes write [] instead of {})
    hooks = fm.get("hooks") or {}
    if not isinstance(hooks, dict):
        hooks = {}

    return Job(
        name=fm.get("name", job_md.parent.name),
        description=fm.get("description", ""),
        schedule=schedule,
        prompt=extract_body(text),
        path=job_md,
        agent=fm.get("agent", ""),
        model=fm.get("model", ""),
        max_iterations=fm.get("max_iterations", 0),
        hooks=hooks,
        enabled=bool(fm.get("enabled", True)),
    )
