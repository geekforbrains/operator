from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

from operator_ai.config import Config
from operator_ai.job_specs import find_job_spec, scan_job_specs
from operator_ai.jobs import Job, _build_job_prompt, _execute_job, scan_jobs
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import MESSAGE_CREATED_AT_KEY
from operator_ai.store import JobState
from operator_ai.tools import memory as memory_tools
from operator_ai.tools.context import get_skill_filter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

JOB_MD = dedent("""\
    ---
    name: daily-digest
    description: Summarize daily activity
    schedule: "0 8 * * *"
    agent: operator
    enabled: true
    ---

    Summarize the daily activity.
""")

JOB_MD_NO_NAME = dedent("""\
    ---
    description: Nameless job
    schedule: "*/5 * * * *"
    enabled: true
    ---

    Do something every 5 minutes.
""")

JOB_MD_DISABLED = dedent("""\
    ---
    name: disabled-job
    description: This job is disabled
    schedule: "0 0 * * *"
    enabled: false
    ---

    Should not run.
""")

JOB_MD_INVALID_SCHEDULE = dedent("""\
    ---
    name: bad-schedule
    description: Bad schedule
    schedule: "not a cron"
    enabled: true
    ---

    Bad.
""")


class FakeStore:
    def __init__(self) -> None:
        self.state = JobState()

    def load_job_state(self, _name: str) -> JobState:
        return self.state

    def save_job_state(self, _name: str, state: JobState) -> None:
        self.state = state

    def ensure_conversation(self, **_kwargs) -> None:
        return None

    def append_messages(self, _conversation_id: str, _messages: list[dict]) -> None:
        return None


def _config() -> Config:
    return Config(
        runtime={"timezone": "America/Vancouver"},
        defaults={"models": ["test/model"]},
        agents={"operator": {}},
    )


def _write_job(jobs_dir: Path, filename: str, content: str) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    p = jobs_dir / filename
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# 5.1 Job spec scanning
# ---------------------------------------------------------------------------


def test_scan_job_specs_finds_flat_md_files(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest.md", JOB_MD)
    _write_job(jobs_dir, "disabled-job.md", JOB_MD_DISABLED)

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 2
    names = {s.name for s in specs}
    assert names == {"daily-digest", "disabled-job"}


def test_scan_job_specs_ignores_subdirectories(tmp_path: Path) -> None:
    """Ensure old-style jobs/<name>/JOB.md pattern is not picked up."""
    jobs_dir = tmp_path / "jobs"
    # Create old-style layout -- should be ignored
    old_dir = jobs_dir / "old-job"
    old_dir.mkdir(parents=True)
    (old_dir / "JOB.md").write_text(JOB_MD)

    # Create new-style flat file
    _write_job(jobs_dir, "new-job.md", JOB_MD)

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].name == "daily-digest"  # name from frontmatter


def test_scan_job_specs_falls_back_to_stem_for_name(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "my-cron.md", JOB_MD_NO_NAME)

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].name == "my-cron"  # uses filename stem when no name in frontmatter


def test_scan_job_specs_empty_dir(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    assert scan_job_specs(jobs_dir) == []


def test_scan_job_specs_nonexistent_dir(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "nonexistent"
    assert scan_job_specs(jobs_dir) == []


def test_scan_job_specs_skips_bad_frontmatter(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "good.md", JOB_MD)
    _write_job(jobs_dir, "bad.md", "no frontmatter here")

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    assert specs[0].name == "daily-digest"


def test_scan_job_specs_reads_all_fields(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    content = dedent("""\
        ---
        name: full-job
        description: A fully specified job
        schedule: "30 9 * * 1-5"
        agent: cora
        model: gpt-4
        enabled: true
        ---

        Do the thing.
    """)
    _write_job(jobs_dir, "full-job.md", content)

    specs = scan_job_specs(jobs_dir)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "full-job"
    assert spec.description == "A fully specified job"
    assert spec.schedule == "30 9 * * 1-5"
    assert spec.agent == "cora"
    assert spec.model == "gpt-4"
    assert spec.enabled is True
    assert spec.path.endswith("full-job.md")


# ---------------------------------------------------------------------------
# 5.1 find_job_spec
# ---------------------------------------------------------------------------


def test_find_job_spec_fast_path(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest.md", JOB_MD)

    spec = find_job_spec("daily-digest", jobs_dir)
    assert spec is not None
    assert spec.name == "daily-digest"


def test_find_job_spec_slow_path(tmp_path: Path) -> None:
    """When frontmatter name differs from filename, slow path finds it."""
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "something-else.md", JOB_MD)  # frontmatter name is "daily-digest"

    spec = find_job_spec("daily-digest", jobs_dir)
    assert spec is not None
    assert spec.name == "daily-digest"


def test_find_job_spec_not_found(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    assert find_job_spec("nonexistent", jobs_dir) is None


# ---------------------------------------------------------------------------
# 5.2 scan_jobs (enriched)
# ---------------------------------------------------------------------------


def test_scan_jobs_flat_files(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest.md", JOB_MD)

    monkeypatch.setattr("operator_ai.job_specs.JOBS_DIR", jobs_dir)
    monkeypatch.setattr("operator_ai.jobs.scan_job_specs", lambda: scan_job_specs(jobs_dir))

    jobs = scan_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "daily-digest"
    assert job.prompt == "Summarize the daily activity."
    assert job.path == jobs_dir / "daily-digest.md"


def test_scan_jobs_skips_invalid_schedule(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "bad-schedule.md", JOB_MD_INVALID_SCHEDULE)

    monkeypatch.setattr("operator_ai.job_specs.JOBS_DIR", jobs_dir)
    monkeypatch.setattr("operator_ai.jobs.scan_job_specs", lambda: scan_job_specs(jobs_dir))

    jobs = scan_jobs()
    assert len(jobs) == 0


def test_scan_jobs_includes_disabled(monkeypatch, tmp_path: Path) -> None:
    """scan_jobs includes disabled jobs (filtering is done at tick time)."""
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "disabled-job.md", JOB_MD_DISABLED)

    monkeypatch.setattr("operator_ai.job_specs.JOBS_DIR", jobs_dir)
    monkeypatch.setattr("operator_ai.jobs.scan_job_specs", lambda: scan_job_specs(jobs_dir))

    jobs = scan_jobs()
    assert len(jobs) == 1
    assert jobs[0].enabled is False


# ---------------------------------------------------------------------------
# 5.2 Job prompt building
# ---------------------------------------------------------------------------


def test_build_job_prompt_includes_rules(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )
    monkeypatch.setattr("operator_ai.prompts.scan_agents", lambda: [])

    store = MemoryStore(base_dir=tmp_path)
    store.create_rule("agent:operator", "Use terse status updates.")

    job = Job(
        name="incident-digest",
        description="Summarize incidents",
        schedule="0 8 * * *",
        prompt="Summarize the latest incidents.",
        path=tmp_path / "incident-digest.md",
    )

    prompt = asyncio.run(
        _build_job_prompt(
            config=_config(),
            job=job,
            agent_name="operator",
            prerun_output="",
            transport=None,
            memory_store=store,
        )
    )

    assert "Use terse status updates." in prompt
    assert "Agent Rules" in prompt


def test_execute_job_configures_memory_and_skill_filter(monkeypatch, tmp_path: Path) -> None:
    async def fake_run_agent(**_kwargs) -> str:
        skill_filter = get_skill_filter()
        assert skill_filter is not None
        assert skill_filter("allowed-skill") is True
        assert skill_filter("blocked-skill") is False
        result = await memory_tools.list_rules(scope="agent")
        assert isinstance(result, str)
        user_message = _kwargs["messages"][1]
        assert user_message[MESSAGE_CREATED_AT_KEY]
        return "done"

    monkeypatch.setattr("operator_ai.agent.run_agent", fake_run_agent)

    store = MemoryStore(base_dir=tmp_path)

    job = Job(
        name="incident-digest",
        description="Summarize incidents",
        schedule="0 8 * * *",
        prompt="Summarize the latest incidents.",
        path=tmp_path / "incident-digest.md",
    )
    config = Config(
        runtime={"timezone": "America/Vancouver"},
        defaults={"models": ["test/model"]},
        agents={"operator": {"permissions": {"skills": ["allowed-skill"]}}},
    )
    fake_store = FakeStore()

    asyncio.run(
        _execute_job(
            job,
            config,
            transports={},
            store=fake_store,
            memory_store=store,
        )
    )

    assert fake_store.state.last_result == "success"


# ---------------------------------------------------------------------------
# 5.3 Job tools (create / update / delete)
# ---------------------------------------------------------------------------


def test_create_job_writes_flat_file(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)
    monkeypatch.setattr("operator_ai.tools.jobs.OPERATOR_DIR", tmp_path)
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: Config(
            runtime={"timezone": "UTC"},
            defaults={"models": ["test/model"]},
            agents={"operator": {}},
        ),
    )

    from operator_ai.tools.jobs import _create_job

    result = _create_job("daily-digest", JOB_MD)
    assert "Created job" in result

    job_file = jobs_dir / "daily-digest.md"
    assert job_file.exists()
    assert job_file.read_text() == JOB_MD

    # Old-style directory should NOT be created
    assert not (jobs_dir / "daily-digest").is_dir()


def test_create_job_rejects_duplicate(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest.md", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)
    monkeypatch.setattr("operator_ai.tools.jobs.OPERATOR_DIR", tmp_path)
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: _config(),
    )

    from operator_ai.tools.jobs import _create_job

    result = _create_job("daily-digest", JOB_MD)
    assert "already exists" in result


def test_update_job_overwrites_flat_file(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest.md", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)
    monkeypatch.setattr("operator_ai.tools.jobs.OPERATOR_DIR", tmp_path)
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: _config(),
    )

    updated = JOB_MD.replace("Summarize daily activity", "Updated description")

    from operator_ai.tools.jobs import _update_job

    result = _update_job("daily-digest", updated)
    assert "Updated job" in result
    assert "Updated description" in (jobs_dir / "daily-digest.md").read_text()


def test_update_job_not_found(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)

    from operator_ai.tools.jobs import _update_job

    result = _update_job("nonexistent", JOB_MD)
    assert "not found" in result


def test_delete_job_removes_flat_file(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest.md", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)

    from operator_ai.tools.jobs import _delete_job

    result = _delete_job("daily-digest")
    assert "Deleted job" in result
    assert not (jobs_dir / "daily-digest.md").exists()


def test_delete_job_not_found(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)

    from operator_ai.tools.jobs import _delete_job

    result = _delete_job("nonexistent")
    assert "not found" in result


def test_toggle_job_enable_disable(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest.md", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)

    from operator_ai.tools.jobs import _toggle_job

    result = _toggle_job("daily-digest", enabled=False)
    assert "Disabled" in result

    # Verify the file was updated
    specs = scan_job_specs(jobs_dir)
    spec = next(s for s in specs if s.name == "daily-digest")
    assert spec.enabled is False

    result = _toggle_job("daily-digest", enabled=True)
    assert "Enabled" in result

    specs = scan_job_specs(jobs_dir)
    spec = next(s for s in specs if s.name == "daily-digest")
    assert spec.enabled is True


# ---------------------------------------------------------------------------
# 5.3 Hook script creation
# ---------------------------------------------------------------------------


def test_create_job_with_hooks_creates_scripts(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr("operator_ai.tools.jobs.JOBS_DIR", jobs_dir)
    monkeypatch.setattr("operator_ai.tools.jobs.OPERATOR_DIR", tmp_path)
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: _config(),
    )

    job_with_hooks = dedent("""\
        ---
        name: hooked-job
        description: Job with hooks
        schedule: "0 8 * * *"
        hooks:
          prerun: scripts/check.sh
          postrun: scripts/notify.sh
        ---

        Do the hooked thing.
    """)

    from operator_ai.tools.jobs import _create_job

    result = _create_job("hooked-job", job_with_hooks)
    assert "Created job" in result

    # Hook scripts are created relative to OPERATOR_DIR (tmp_path here)
    prerun = tmp_path / "scripts" / "check.sh"
    postrun = tmp_path / "scripts" / "notify.sh"
    assert prerun.exists()
    assert postrun.exists()
    assert prerun.read_text().startswith("#!/bin/bash")
    assert postrun.read_text().startswith("#!/bin/bash")


# ---------------------------------------------------------------------------
# Frontmatter validation
# ---------------------------------------------------------------------------


def test_validate_frontmatter_rejects_missing_schedule(monkeypatch) -> None:
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: _config(),
    )

    from operator_ai.tools.jobs import _validate_frontmatter

    result = _validate_frontmatter({"description": "no schedule"})
    assert result is not None
    assert "schedule" in result


def test_validate_frontmatter_rejects_invalid_cron(monkeypatch) -> None:
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: _config(),
    )

    from operator_ai.tools.jobs import _validate_frontmatter

    result = _validate_frontmatter({"schedule": "bad cron"})
    assert result is not None
    assert "invalid cron" in result


def test_validate_frontmatter_rejects_unknown_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: _config(),
    )

    from operator_ai.tools.jobs import _validate_frontmatter

    result = _validate_frontmatter({"schedule": "0 8 * * *", "agent": "nonexistent"})
    assert result is not None
    assert "unknown agent" in result


def test_validate_frontmatter_accepts_valid(monkeypatch) -> None:
    monkeypatch.setattr(
        "operator_ai.tools.jobs.load_config",
        lambda: _config(),
    )

    from operator_ai.tools.jobs import _validate_frontmatter

    result = _validate_frontmatter({"schedule": "0 8 * * *", "agent": "operator"})
    assert result is None
