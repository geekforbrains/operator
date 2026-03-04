from __future__ import annotations

from pathlib import Path

from operator_ai.job_specs import find_job_spec, scan_job_specs


def _write_job(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_scan_job_specs_reads_frontmatter_fields(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir / "daily" / "JOB.md",
        """---
name: daily-summary
schedule: "0 9 * * *"
agent: operator
enabled: false
description: Morning digest
model: "openai/gpt-4o"
---
Run a summary.
""",
    )

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "daily-summary"
    assert spec.schedule == "0 9 * * *"
    assert spec.agent == "operator"
    assert spec.enabled is False
    assert spec.description == "Morning digest"
    assert spec.model == "openai/gpt-4o"
    assert spec.path.endswith("daily/JOB.md")


def test_model_defaults_to_empty_when_omitted(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir / "basic" / "JOB.md",
        '---\nschedule: "0 9 * * *"\n---\nDo stuff.\n',
    )

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].model == ""


def test_scan_job_specs_ignores_invalid_frontmatter(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir / "bad" / "JOB.md",
        """---
name: bad
schedule: [not valid
---
Body
""",
    )
    _write_job(
        jobs_dir / "missing" / "JOB.md",
        "No frontmatter at all",
    )

    specs = scan_job_specs(jobs_dir)
    assert specs == []


def test_find_job_spec_uses_frontmatter_name(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir / "folder-name" / "JOB.md",
        """---
name: release-audit
schedule: "*/15 * * * *"
---
Body
""",
    )

    assert find_job_spec("folder-name", jobs_dir) is None
    spec = find_job_spec("release-audit", jobs_dir)
    assert spec is not None
    assert spec.name == "release-audit"
