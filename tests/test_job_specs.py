from __future__ import annotations

from pathlib import Path

from operator_ai.job_specs import find_job_spec, scan_job_specs


def _write_job(jobs_dir: Path, filename: str, content: str) -> None:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / filename).write_text(content)


def test_scan_job_specs_reads_frontmatter_fields(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "daily.md",
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
    assert spec.path.endswith("daily.md")


def test_model_defaults_to_empty_when_omitted(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "basic.md",
        '---\nschedule: "0 9 * * *"\n---\nDo stuff.\n',
    )

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].model == ""


def test_scan_job_specs_ignores_invalid_frontmatter(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "bad.md",
        """---
name: bad
schedule: [not valid
---
Body
""",
    )
    _write_job(
        jobs_dir,
        "missing.md",
        "No frontmatter at all",
    )

    specs = scan_job_specs(jobs_dir)
    assert specs == []


def test_find_job_spec_uses_frontmatter_name(tmp_path: Path) -> None:
    """When frontmatter name differs from filename, slow path finds it."""
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "some-file.md",
        """---
name: release-audit
schedule: "*/15 * * * *"
---
Body
""",
    )

    # Fast path uses filename stem, so "some-file" won't match "release-audit"
    assert find_job_spec("some-file", jobs_dir) is None
    spec = find_job_spec("release-audit", jobs_dir)
    assert spec is not None
    assert spec.name == "release-audit"
