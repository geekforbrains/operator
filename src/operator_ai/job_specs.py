from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from operator_ai.config import OPERATOR_DIR
from operator_ai.frontmatter import parse_frontmatter

logger = logging.getLogger("operator.job_specs")

JOBS_DIR = OPERATOR_DIR / "jobs"


@dataclass(frozen=True)
class JobSpec:
    name: str
    schedule: str
    enabled: bool
    description: str
    path: str  # absolute path to JOB.md
    agent: str = ""
    model: str = ""


def scan_job_specs(jobs_dir: Path = JOBS_DIR) -> list[JobSpec]:
    """Scan jobs/<name>/JOB.md for frontmatter."""
    if not jobs_dir.is_dir():
        return []

    specs: list[JobSpec] = []
    for child in sorted(jobs_dir.iterdir()):
        if not child.is_dir():
            continue
        job_md = child / "JOB.md"
        if not job_md.exists():
            continue
        spec = _read_job_spec(job_md)
        if spec is not None:
            specs.append(spec)

    return specs


def _read_job_spec(job_md: Path) -> JobSpec | None:
    """Parse a single JOB.md file into a JobSpec, or None on failure."""
    try:
        frontmatter = parse_frontmatter(job_md.read_text())
    except Exception:
        logger.warning("Failed to parse job frontmatter in %s", job_md)
        return None
    if not frontmatter:
        return None

    fallback_name = job_md.parent.name  # directory name
    return JobSpec(
        name=frontmatter.get("name", fallback_name),
        schedule=frontmatter.get("schedule", ""),
        agent=frontmatter.get("agent", ""),
        model=frontmatter.get("model", ""),
        enabled=bool(frontmatter.get("enabled", True)),
        description=frontmatter.get("description", ""),
        path=str(job_md),
    )


def find_job_spec(name: str, jobs_dir: Path = JOBS_DIR) -> JobSpec | None:
    """Find a single job spec by name.

    Fast path: try reading jobs/<name>/JOB.md directly.
    Slow path: scan all specs if the fast path misses (frontmatter name differs from dir name).
    """
    job_md = jobs_dir / name / "JOB.md"
    if job_md.is_file():
        spec = _read_job_spec(job_md)
        if spec is not None and spec.name == name:
            return spec

    # Frontmatter name may differ from directory name -- fall back to scan
    for spec in scan_job_specs(jobs_dir):
        if spec.name == name:
            return spec
    return None
