from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

from operator_ai.config import Config
from operator_ai.job import Job, execute_job, find_job, scan_jobs
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import MESSAGE_CREATED_AT_KEY
from operator_ai.run_prompt import JobEnvelope, build_agent_system_prompt
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


def _config(base_dir: Path | None = None) -> Config:
    config = Config(
        defaults={"models": ["test/model"]},
        agents={"operator": {}},
    )
    if base_dir is not None:
        config.set_base_dir(base_dir)
    return config


def _write_job(jobs_dir: Path, name: str, content: str) -> Path:
    job_dir = jobs_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    p = job_dir / "JOB.md"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# scan_jobs
# ---------------------------------------------------------------------------


def test_scan_jobs_finds_job_dirs(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest", JOB_MD)
    _write_job(jobs_dir, "disabled-job", JOB_MD_DISABLED)

    jobs = scan_jobs(jobs_dir)
    assert len(jobs) == 2
    names = {j.name for j in jobs}
    assert names == {"daily-digest", "disabled-job"}


def test_scan_jobs_reads_all_fields(tmp_path: Path) -> None:
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
    _write_job(jobs_dir, "full-job", content)

    jobs = scan_jobs(jobs_dir)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "full-job"
    assert job.description == "A fully specified job"
    assert job.schedule == "30 9 * * 1-5"
    assert job.agent == "cora"
    assert job.model == "gpt-4"
    assert job.enabled is True
    assert job.prompt == "Do the thing."
    assert job.path == jobs_dir / "full-job" / "JOB.md"


def test_scan_jobs_ignores_flat_files(tmp_path: Path) -> None:
    """Flat .md files at jobs/ root are ignored."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "stray.md").write_text(JOB_MD)

    _write_job(jobs_dir, "real-job", JOB_MD)
    jobs = scan_jobs(jobs_dir)
    assert len(jobs) == 1
    assert jobs[0].name == "daily-digest"


def test_scan_jobs_falls_back_to_dir_name(tmp_path: Path) -> None:
    """When no name in frontmatter, directory name is used."""
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "my-cron", JOB_MD_NO_NAME)

    jobs = scan_jobs(jobs_dir)
    assert len(jobs) == 1
    assert jobs[0].name == "my-cron"


def test_scan_jobs_empty_dir(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    assert scan_jobs(jobs_dir) == []


def test_scan_jobs_nonexistent_dir(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "nonexistent"
    assert scan_jobs(jobs_dir) == []


def test_scan_jobs_skips_bad_frontmatter(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "good", JOB_MD)

    bad_dir = jobs_dir / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "JOB.md").write_text("no frontmatter here")

    jobs = scan_jobs(jobs_dir)
    assert len(jobs) == 1
    assert jobs[0].name == "daily-digest"


def test_scan_jobs_skips_invalid_schedule(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "bad-schedule", JOB_MD_INVALID_SCHEDULE)
    assert scan_jobs(jobs_dir) == []


def test_scan_jobs_includes_disabled(tmp_path: Path) -> None:
    """scan_jobs includes disabled jobs (filtering is done at tick time)."""
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "disabled-job", JOB_MD_DISABLED)

    jobs = scan_jobs(jobs_dir)
    assert len(jobs) == 1
    assert jobs[0].enabled is False


def test_scan_jobs_model_defaults_to_empty(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "basic", '---\nschedule: "0 9 * * *"\n---\nDo stuff.\n')

    jobs = scan_jobs(jobs_dir)
    assert len(jobs) == 1
    assert jobs[0].model == ""


def test_scan_jobs_ignores_invalid_yaml(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "bad", "---\nname: bad\nschedule: [not valid\n---\nBody\n")
    no_fm_dir = jobs_dir / "missing"
    no_fm_dir.mkdir(parents=True)
    (no_fm_dir / "JOB.md").write_text("No frontmatter at all")

    assert scan_jobs(jobs_dir) == []


# ---------------------------------------------------------------------------
# find_job
# ---------------------------------------------------------------------------


def test_find_job_fast_path(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest", JOB_MD)

    job = find_job("daily-digest", jobs_dir)
    assert job is not None
    assert job.name == "daily-digest"


def test_find_job_slow_path(tmp_path: Path) -> None:
    """When frontmatter name differs from dir name, slow path finds it."""
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "something-else", JOB_MD)  # frontmatter name is "daily-digest"

    job = find_job("daily-digest", jobs_dir)
    assert job is not None
    assert job.name == "daily-digest"


def test_find_job_not_found(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    assert find_job("nonexistent", jobs_dir) is None


def test_find_job_dir_name_mismatch(tmp_path: Path) -> None:
    """When frontmatter name differs from directory name, find by dir name returns None."""
    jobs_dir = tmp_path / "jobs"
    _write_job(
        jobs_dir,
        "some-dir",
        '---\nname: release-audit\nschedule: "*/15 * * * *"\n---\nBody\n',
    )
    assert find_job("some-dir", jobs_dir) is None
    job = find_job("release-audit", jobs_dir)
    assert job is not None
    assert job.name == "release-audit"


# ---------------------------------------------------------------------------
# Job prompt building
# ---------------------------------------------------------------------------


def test_build_job_prompt_includes_rules(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )
    monkeypatch.setattr("operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: [])

    store = MemoryStore(base_dir=tmp_path)
    store.upsert_rule("agent:operator", "status-style", "Use terse status updates.")

    job = Job(
        name="incident-digest",
        description="Summarize incidents",
        schedule="0 8 * * *",
        prompt="Summarize the latest incidents.",
        path=tmp_path / "incident-digest" / "JOB.md",
    )

    prompt = build_agent_system_prompt(
        config=_config(tmp_path),
        agent_name="operator",
        memory_store=store,
        skill_filter=_config(tmp_path).agent_skill_filter("operator"),
        run_envelope=JobEnvelope(
            name=job.name,
            description=job.description,
            schedule=job.schedule,
            path=job.path,
        ),
    )

    assert "Use terse status updates." in prompt
    assert "Agent Rules" in prompt


def test_build_job_prompt_includes_prerun_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.prompts.load_system_prompt", lambda _path=None: "# System")
    monkeypatch.setattr(
        "operator_ai.prompts.load_agent_prompt",
        lambda _config, agent_name: f"# Agent\n\n{agent_name}",
    )
    monkeypatch.setattr(
        "operator_ai.prompts.load_skills_prompt",
        lambda _skills_dir, **_kwargs: "",
    )
    monkeypatch.setattr("operator_ai.prompts.load_configured_agents", lambda *_args, **_kwargs: [])

    job = Job(
        name="scripted-job",
        description="Job with prerun",
        schedule="0 8 * * *",
        prompt="Process the data.",
        path=tmp_path / "scripted-job" / "JOB.md",
    )

    prompt = build_agent_system_prompt(
        config=_config(tmp_path),
        agent_name="operator",
        skill_filter=_config(tmp_path).agent_skill_filter("operator"),
        run_envelope=JobEnvelope(
            name=job.name,
            description=job.description,
            schedule=job.schedule,
            path=job.path,
            prerun_output="fetched 42 items\nall healthy",
        ),
    )

    assert "<prerun_output>" in prompt
    assert "fetched 42 items" in prompt
    assert "all healthy" in prompt


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
        path=tmp_path / "incident-digest" / "JOB.md",
    )
    config = Config(
        defaults={"models": ["test/model"]},
        agents={"operator": {"permissions": {"skills": ["allowed-skill"]}}},
    )
    config.set_base_dir(tmp_path)
    fake_store = FakeStore()

    asyncio.run(
        execute_job(
            job,
            config,
            transports={},
            store=fake_store,
            memory_store=store,
        )
    )

    assert fake_store.state.last_result == "success"


# ---------------------------------------------------------------------------
# Job tools (create / update / delete / enable / disable / list)
# ---------------------------------------------------------------------------


def test_create_job_writes_directory(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)
    monkeypatch.setattr("operator_ai.tools.jobs.load_config", lambda: _config())

    from operator_ai.tools.jobs import create_job

    result = asyncio.run(
        create_job(
            name="daily-digest",
            schedule="0 8 * * *",
            prompt="Summarize the daily activity.",
            description="Summarize daily activity",
            agent="operator",
        )
    )
    assert "Created job" in result

    job_dir = jobs_dir / "daily-digest"
    assert job_dir.is_dir()
    job_md = job_dir / "JOB.md"
    assert job_md.exists()
    content = job_md.read_text()
    assert "schedule:" in content
    assert "0 8 * * *" in content
    assert "Summarize the daily activity." in content


def test_create_job_rejects_duplicate(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)
    monkeypatch.setattr("operator_ai.tools.jobs.load_config", lambda: _config())

    from operator_ai.tools.jobs import create_job

    result = asyncio.run(create_job(name="daily-digest", schedule="0 8 * * *", prompt="test"))
    assert "already exists" in result


def test_create_job_rejects_missing_schedule(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)

    from operator_ai.tools.jobs import create_job

    result = asyncio.run(create_job(name="test", schedule="", prompt="test"))
    assert "schedule is required" in result


def test_create_job_rejects_invalid_cron(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)

    from operator_ai.tools.jobs import create_job

    result = asyncio.run(create_job(name="test", schedule="bad cron", prompt="test"))
    assert "invalid cron" in result


def test_create_job_rejects_unknown_agent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)
    monkeypatch.setattr("operator_ai.tools.jobs.load_config", lambda: _config())

    from operator_ai.tools.jobs import create_job

    result = asyncio.run(
        create_job(name="test", schedule="0 8 * * *", prompt="test", agent="nonexistent")
    )
    assert "unknown agent" in result


def test_create_job_rejects_missing_prompt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)

    from operator_ai.tools.jobs import create_job

    result = asyncio.run(create_job(name="test", schedule="0 8 * * *", prompt=""))
    assert "prompt is required" in result


def test_update_job_overwrites_file(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)
    monkeypatch.setattr("operator_ai.tools.jobs.load_config", lambda: _config())

    from operator_ai.tools.jobs import update_job

    result = asyncio.run(
        update_job(
            name="daily-digest",
            schedule="0 9 * * *",
            prompt="Updated prompt.",
            description="Updated description",
        )
    )
    assert "Updated job" in result

    content = (jobs_dir / "daily-digest" / "JOB.md").read_text()
    assert "0 9 * * *" in content
    assert "Updated prompt." in content
    assert "Updated description" in content


def test_update_job_not_found(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)

    from operator_ai.tools.jobs import update_job

    result = asyncio.run(update_job(name="nonexistent", schedule="0 8 * * *", prompt="test"))
    assert "not found" in result


def test_delete_job_removes_directory(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)

    from operator_ai.tools.jobs import delete_job

    result = asyncio.run(delete_job(name="daily-digest"))
    assert "Deleted job" in result
    assert not (jobs_dir / "daily-digest").exists()


def test_delete_job_not_found(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)

    from operator_ai.tools.jobs import delete_job

    result = asyncio.run(delete_job(name="nonexistent"))
    assert "not found" in result


def test_enable_disable_job(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    _write_job(jobs_dir, "daily-digest", JOB_MD)
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)

    from operator_ai.tools.jobs import disable_job, enable_job

    result = asyncio.run(disable_job(name="daily-digest"))
    assert "Disabled" in result

    jobs = scan_jobs(jobs_dir)
    job = next(j for j in jobs if j.name == "daily-digest")
    assert job.enabled is False

    result = asyncio.run(enable_job(name="daily-digest"))
    assert "Enabled" in result

    jobs = scan_jobs(jobs_dir)
    job = next(j for j in jobs if j.name == "daily-digest")
    assert job.enabled is True


# ---------------------------------------------------------------------------
# Hook script creation
# ---------------------------------------------------------------------------


def test_create_job_with_hooks_creates_scripts(monkeypatch, tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr("operator_ai.tools.jobs.get_base_dir", lambda: tmp_path)
    monkeypatch.setattr("operator_ai.tools.jobs.load_config", lambda: _config())

    from operator_ai.tools.jobs import create_job

    result = asyncio.run(
        create_job(
            name="hooked-job",
            schedule="0 8 * * *",
            prompt="Do the hooked thing.",
            description="Job with hooks",
            prerun=True,
            postrun=True,
        )
    )
    assert "Created job" in result

    job_dir = jobs_dir / "hooked-job"
    prerun = job_dir / "scripts" / "prerun.sh"
    postrun = job_dir / "scripts" / "postrun.sh"
    assert prerun.exists()
    assert postrun.exists()
    assert prerun.read_text().startswith("#!/bin/bash")
    assert postrun.read_text().startswith("#!/bin/bash")

    job_md = job_dir / "JOB.md"
    content = job_md.read_text()
    assert "prerun" in content
    assert "scripts/prerun.sh" in content
    assert "postrun" in content
    assert "scripts/postrun.sh" in content


# ---------------------------------------------------------------------------
# File assembly
# ---------------------------------------------------------------------------


def test_build_job_file_minimal() -> None:
    from operator_ai.tools.jobs import _build_job_file

    content = _build_job_file(
        name="test-job",
        schedule="0 8 * * *",
        prompt="Do the thing.",
    )
    assert "---" in content
    assert "name: test-job" in content
    assert "schedule: 0 8 * * *" in content or "schedule: '0 8 * * *'" in content
    assert "Do the thing." in content
    assert "enabled: true" in content


def test_build_job_file_full() -> None:
    from operator_ai.tools.jobs import _build_job_file

    content = _build_job_file(
        name="full-job",
        schedule="0 8 * * *",
        prompt="Full prompt.",
        description="Full description",
        agent="operator",
        model="gpt-4",
        max_iterations=15,
        enabled=True,
        prerun="scripts/prerun.sh",
        postrun="scripts/postrun.sh",
    )
    assert "name: full-job" in content
    assert "description: Full description" in content
    assert "agent: operator" in content
    assert "model: gpt-4" in content
    assert "max_iterations: 15" in content
    assert "prerun:" in content
    assert "postrun:" in content
    assert "Full prompt." in content


def test_build_job_file_omits_empty_optional_fields() -> None:
    from operator_ai.tools.jobs import _build_job_file

    content = _build_job_file(
        name="minimal-job",
        schedule="0 8 * * *",
        prompt="Minimal.",
    )
    assert "agent:" not in content
    assert "model:" not in content
    assert "max_iterations:" not in content
    assert "hooks:" not in content
    assert "prerun:" not in content
    assert "postrun:" not in content
