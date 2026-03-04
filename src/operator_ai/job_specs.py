from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from operator_ai.config import OPERATOR_DIR
from operator_ai.skills import parse_frontmatter

logger = logging.getLogger("operator.job_specs")

JOBS_DIR = OPERATOR_DIR / "jobs"


@dataclass(frozen=True)
class JobSpec:
    name: str
    schedule: str
    enabled: bool
    description: str
    path: str
    agent: str = ""
    model: str = ""


def scan_job_specs(jobs_dir: Path = JOBS_DIR) -> list[JobSpec]:
    """Read lightweight job metadata from JOB.md frontmatter."""
    if not jobs_dir.is_dir():
        return []

    specs: list[JobSpec] = []
    for job_md in sorted(jobs_dir.glob("*/JOB.md")):
        try:
            frontmatter = parse_frontmatter(job_md.read_text())
        except Exception:
            logger.warning("Failed to parse job frontmatter in %s", job_md)
            continue

        if not frontmatter:
            continue

        specs.append(
            JobSpec(
                name=frontmatter.get("name", job_md.parent.name),
                schedule=frontmatter.get("schedule", ""),
                agent=frontmatter.get("agent", ""),
                model=frontmatter.get("model", ""),
                enabled=bool(frontmatter.get("enabled", True)),
                description=frontmatter.get("description", ""),
                path=str(job_md),
            )
        )

    return specs


def find_job_spec(name: str, jobs_dir: Path = JOBS_DIR) -> JobSpec | None:
    for spec in scan_job_specs(jobs_dir):
        if spec.name == name:
            return spec
    return None
