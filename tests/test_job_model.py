"""Tests for per-job model override via JOB.md frontmatter."""

from __future__ import annotations

from pathlib import Path

from operator_ai.job_specs import JobSpec, scan_job_specs

# -- helpers ----------------------------------------------------------------


def _write_job(jobs_dir: Path, name: str, frontmatter: str, body: str = "Do stuff.") -> None:
    d = jobs_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "JOB.md").write_text(f"---\n{frontmatter}\n---\n{body}\n")


SCHEDULE = 'schedule: "0 9 * * *"'


# -- scan_job_specs (frontmatter parsing) -----------------------------------


def test_model_parsed_from_frontmatter(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "a", f'{SCHEDULE}\nmodel: "openai/gpt-4o"')

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].model == "openai/gpt-4o"


def test_model_defaults_to_empty_when_omitted(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "b", SCHEDULE)

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].model == ""


# -- model resolution logic -------------------------------------------------


def test_job_model_produces_single_element_list() -> None:
    """When job.model is set, resolution should yield [model] (not config fallback)."""
    job_model = "anthropic/claude-haiku-4-5-20251001"
    config_models = ["anthropic/claude-opus-4-5", "openai/gpt-4o"]

    # This mirrors the resolution line in _execute_job:
    #   models = [job.model] if job.model else config.agent_models(agent_name)
    models = [job_model] if job_model else config_models
    assert models == ["anthropic/claude-haiku-4-5-20251001"]


def test_empty_job_model_falls_back_to_config() -> None:
    """When job.model is empty, resolution should use config models."""
    job_model = ""
    config_models = ["anthropic/claude-opus-4-5", "openai/gpt-4o"]

    models = [job_model] if job_model else config_models
    assert models == config_models


# -- JobSpec dataclass field ------------------------------------------------


def test_job_spec_model_field_default() -> None:
    spec = JobSpec(name="x", schedule="* * * * *", enabled=True, description="", path="/tmp")
    assert spec.model == ""
