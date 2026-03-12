from __future__ import annotations

from pathlib import Path

from operator_ai.job_specs import find_job_spec, scan_job_specs


def _write_job(jobs_dir: Path, name: str, content: str) -> None:
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "JOB.md").write_text(content)


def test_scan_job_specs_reads_frontmatter_fields(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "daily-summary",
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
    assert spec.path.endswith("JOB.md")


def test_model_defaults_to_empty_when_omitted(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "basic",
        '---\nschedule: "0 9 * * *"\n---\nDo stuff.\n',
    )

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].model == ""


def test_scan_job_specs_ignores_invalid_frontmatter(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "bad",
        """---
name: bad
schedule: [not valid
---
Body
""",
    )

    # File without frontmatter — write directly (not a proper job)
    no_fm_dir = jobs_dir / "missing"
    no_fm_dir.mkdir(parents=True)
    (no_fm_dir / "JOB.md").write_text("No frontmatter at all")

    specs = scan_job_specs(jobs_dir)
    assert specs == []


def test_scan_job_specs_ignores_flat_files(tmp_path: Path) -> None:
    """Flat .md files in jobs/ should not be picked up."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "stray.md").write_text('---\nname: stray\nschedule: "0 9 * * *"\n---\nBody\n')

    specs = scan_job_specs(jobs_dir)
    assert specs == []


def test_find_job_spec_uses_frontmatter_name(tmp_path: Path) -> None:
    """When frontmatter name differs from directory name, slow path finds it."""
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "some-dir",
        """---
name: release-audit
schedule: "*/15 * * * *"
---
Body
""",
    )

    # Fast path uses directory name, so "some-dir" won't match "release-audit"
    assert find_job_spec("some-dir", jobs_dir) is None
    spec = find_job_spec("release-audit", jobs_dir)
    assert spec is not None
    assert spec.name == "release-audit"


def test_find_job_spec_fast_path(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "daily-digest",
        '---\nname: daily-digest\nschedule: "0 8 * * *"\n---\nBody\n',
    )

    spec = find_job_spec("daily-digest", jobs_dir)
    assert spec is not None
    assert spec.name == "daily-digest"


def test_scan_job_specs_falls_back_to_dir_name(tmp_path: Path) -> None:
    """When no name in frontmatter, directory name is used."""
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "my-cron",
        '---\nschedule: "*/5 * * * *"\nenabled: true\n---\nDo something.\n',
    )

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].name == "my-cron"
