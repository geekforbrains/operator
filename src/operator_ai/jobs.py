from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from croniter import croniter

from operator_ai.config import OPERATOR_DIR, Config
from operator_ai.job_specs import JOBS_DIR
from operator_ai.log_context import new_run_id, set_run_context
from operator_ai.memory import MemoryStore
from operator_ai.prompts import assemble_system_prompt, load_prompt
from operator_ai.skills import extract_body, parse_frontmatter
from operator_ai.store import DB_PATH, Store
from operator_ai.transport.base import Transport

logger = logging.getLogger("operator.jobs")


@dataclass
class Job:
    name: str
    description: str
    schedule: str
    prompt: str
    job_dir: Path
    agent: str = ""
    model: str = ""
    max_iterations: int = 0
    hooks: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def scan_jobs() -> list[Job]:
    """Scan jobs/*/JOB.md, parse frontmatter, validate schedule, return jobs."""
    jobs: list[Job] = []
    if not JOBS_DIR.is_dir():
        return jobs

    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        job_md = job_dir / "JOB.md"
        if not job_md.exists():
            continue
        try:
            text = job_md.read_text()
            fm = parse_frontmatter(text)
            if not fm:
                logger.warning("No frontmatter in %s, skipping", job_md)
                continue

            schedule = fm.get("schedule", "")
            if not schedule or not croniter.is_valid(schedule):
                logger.warning("Invalid schedule '%s' in %s, skipping", schedule, job_md)
                continue

            body = extract_body(text)

            # Coerce hooks to dict (agents sometimes write [] instead of {})
            hooks = fm.get("hooks") or {}
            if not isinstance(hooks, dict):
                hooks = {}

            jobs.append(
                Job(
                    name=fm.get("name", job_dir.name),
                    description=fm.get("description", ""),
                    schedule=schedule,
                    prompt=body,
                    job_dir=job_dir,
                    agent=fm.get("agent", ""),
                    model=fm.get("model", ""),
                    max_iterations=fm.get("max_iterations", 0),
                    hooks=hooks,
                    enabled=fm.get("enabled", True),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse %s: %s", job_md, e)

    return jobs


async def _run_hook(
    job: Job,
    hook_name: str,
    agent_name: str = "",
    stdin_data: str = "",
    timeout: int = 30,
) -> tuple[int, str]:
    """Run a hook script from the job's scripts/ directory."""
    script_path = job.hooks.get(hook_name, "")
    if not script_path:
        return 0, ""

    full_path = _resolve_hook_script_path(job, hook_name, script_path)
    if full_path is None:
        return 1, f"[invalid {hook_name} hook path: {script_path}]"
    if not full_path.exists():
        logger.warning("Hook script not found: %s", full_path)
        return 1, f"[hook script not found: {full_path}]"
    if not full_path.is_file():
        logger.warning("Hook script is not a file: %s", full_path)
        return 1, f"[hook script is not a file: {full_path}]"

    env = {
        **os.environ,
        "JOB_NAME": job.name,
        "OPERATOR_AGENT": agent_name or job.agent,
        "OPERATOR_HOME": str(OPERATOR_DIR),
        "OPERATOR_DB": str(DB_PATH),
    }

    logger.debug("Running %s hook for job '%s': %s", hook_name, job.name, full_path)
    hook_start = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            str(full_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(job.job_dir),
            env=env,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode() if stdin_data else None),
            timeout=timeout,
        )
        output = stdout.decode(errors="replace")
        elapsed = round(time.time() - hook_start, 1)
        logger.info(
            "Hook %s for job '%s' exited %d in %.1fs%s",
            hook_name,
            job.name,
            proc.returncode or 0,
            elapsed,
            f" — {output.strip()}" if output.strip() else "",
        )
        return proc.returncode or 0, output
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        elapsed = round(time.time() - hook_start, 1)
        logger.warning("Hook %s for job '%s' timed out after %ds", hook_name, job.name, timeout)
        return 1, f"[hook timed out after {timeout}s]"
    except Exception as e:
        elapsed = round(time.time() - hook_start, 1)
        logger.exception("Hook %s for job '%s' failed after %.1fs", hook_name, job.name, elapsed)
        return 1, f"[hook error: {e}]"


def _job_memory_scopes(agent_name: str) -> list[tuple[str, str]]:
    return [("agent", agent_name), ("global", "global")]


async def _job_memory_context(
    job: Job,
    agent_name: str,
    memory_store: MemoryStore | None,
) -> tuple[list[str], list[str]]:
    if memory_store is None:
        return [], []

    pinned_lines: list[str] = []
    scopes = _job_memory_scopes(agent_name)
    for scope, scope_id in scopes:
        for memory in memory_store.get_pinned_memories(scope, scope_id):
            pinned_lines.append(f"- [{memory['scope']}] {memory['content']}")

    context_sections: list[str] = []
    relevant = await memory_store.search(job.prompt, scopes)
    if relevant:
        context_sections.append(
            '<context_snapshot source="memories">\n'
            "Relevant memories from previous work:\n"
            + "\n".join(f"- {item['content']}" for item in relevant)
            + "\n</context_snapshot>"
        )

    return pinned_lines, context_sections


async def _build_job_prompt(
    config: Config,
    job: Job,
    agent_name: str,
    prerun_output: str,
    transport: Transport | None,
    memory_store: MemoryStore | None = None,
) -> str:
    """Assemble the system prompt for a job execution."""
    workspace = config.agent_workspace(agent_name)
    job_details = (
        f"- Name: {job.name}\n"
        f"- Schedule: `{job.schedule}`\n"
        f"- Description: {job.description}\n"
        f"- Job directory: `{job.job_dir}`\n"
        f"- Workspace: `{workspace}`\n"
        f"- Operator home: `{OPERATOR_DIR}` (also `$OPERATOR_HOME`)"
    )
    job_ctx = load_prompt("job.md").replace("{job_details}", job_details)

    pinned_lines, memory_sections = await _job_memory_context(job, agent_name, memory_store)

    context_sections: list[str] = [job_ctx]
    context_sections.extend(memory_sections)

    if prerun_output:
        context_sections.append(f"<prerun_output>\n{prerun_output}\n</prerun_output>")
    return assemble_system_prompt(
        config=config,
        agent_name=agent_name,
        context_sections=context_sections,
        pinned_memory_lines=pinned_lines,
        transport_extra=transport.get_prompt_extra() if transport else "",
        skill_filter=config.agent_skill_filter(agent_name),
    )


def _resolve_hook_script_path(job: Job, hook_name: str, script_path: str) -> Path | None:
    try:
        rel_path = Path(script_path)
    except Exception:
        logger.warning("Invalid %s hook path in job '%s': %r", hook_name, job.name, script_path)
        return None

    if rel_path.is_absolute():
        logger.warning(
            "Absolute %s hook path is not allowed in job '%s': %s",
            hook_name,
            job.name,
            script_path,
        )
        return None

    try:
        resolved = (job.job_dir / rel_path).resolve()
        resolved.relative_to(job.job_dir.resolve())
        return resolved
    except Exception:
        logger.warning(
            "Out-of-job-dir %s hook path is not allowed in job '%s': %s",
            hook_name,
            job.name,
            script_path,
        )
        return None


async def _execute_job(
    job: Job,
    config: Config,
    transports: dict[str, Transport],
    store: Store,
    memory_store: MemoryStore | None = None,
) -> None:
    """Full execution: prerun gate -> agent -> postrun -> state."""
    start_time = time.time()
    agent_name = job.agent or config.default_agent()
    set_run_context(agent=agent_name, run_id=new_run_id())
    state = store.load_job_state(job.name)
    conversation_id = f"job:{job.name}:{int(start_time)}"
    messages: list[dict[str, Any]] = []
    persisted_count = 0

    try:
        # Prerun gate
        prerun_output = ""
        if job.hooks.get("prerun"):
            exit_code, prerun_output = await _run_hook(job, "prerun", agent_name=agent_name)
            if exit_code != 0:
                logger.info(
                    "Job '%s' gated by prerun hook (exit %d)%s",
                    job.name,
                    exit_code,
                    f": {prerun_output.strip()}" if prerun_output.strip() else "",
                )
                state.last_run = datetime.now(UTC).isoformat()
                state.last_result = "gated"
                state.last_duration_seconds = round(time.time() - start_time, 1)
                state.gate_count += 1
                store.save_job_state(job.name, state)
                return

        # Lazy imports to avoid circular dependency:
        # jobs -> agent -> tools/__init__ -> tools/jobs -> jobs
        from operator_ai.agent import run_agent
        from operator_ai.tools import kv as kv_tools
        from operator_ai.tools import memory as memory_tools
        from operator_ai.tools import messaging
        from operator_ai.tools.context import set_skill_filter

        transport = transports.get(agent_name)
        system_prompt = await _build_job_prompt(
            config,
            job,
            agent_name,
            prerun_output,
            transport,
            memory_store=memory_store,
        )
        store.ensure_conversation(
            conversation_id=conversation_id,
            transport_name="job",
            channel_id="",
            root_thread_id=job.name,
            metadata={"job_name": job.name, "agent": agent_name},
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": job.prompt},
        ]
        store.append_messages(conversation_id, messages)
        persisted_count = len(messages)

        # Configure tools with execution context
        messaging.configure({"transport": transport})
        kv_tools.configure({"agent_name": agent_name})
        memory_tools.configure(
            {
                "memory_store": memory_store,
                "user_id": "",
                "agent_name": agent_name,
                "allow_user_scope": False,
            }
        )
        set_skill_filter(config.agent_skill_filter(agent_name))

        models = [job.model] if job.model else config.agent_models(agent_name)
        max_iter = job.max_iterations or config.agent_max_iterations(agent_name)

        extra_tools = transport.get_tools() if transport else None
        output = await run_agent(
            messages=messages,
            models=models,
            max_iterations=max_iter,
            workspace=str(config.agent_workspace(agent_name)),
            agent_name=agent_name,
            context_ratio=config.agent_context_ratio(agent_name),
            max_output_tokens=config.agent_max_output_tokens(agent_name),
            thinking=config.agent_thinking(agent_name),
            extra_tools=extra_tools,
            tool_filter=config.agent_tool_filter(agent_name),
            shared_dir=config.shared_dir,
            sandboxed=config.agent_sandboxed(agent_name),
            config=config,
        )

        # Postrun hook
        if job.hooks.get("postrun"):
            exit_code, postrun_output = await _run_hook(
                job, "postrun", agent_name=agent_name, stdin_data=output
            )
            if exit_code != 0:
                details = f": {postrun_output.strip()}" if postrun_output.strip() else ""
                raise RuntimeError(f"postrun hook exited {exit_code}{details}")

        logger.info("Job '%s' completed in %.1fs", job.name, time.time() - start_time)
        state.last_run = datetime.now(UTC).isoformat()
        state.last_result = "success"
        state.last_duration_seconds = round(time.time() - start_time, 1)
        state.last_error = ""
        state.run_count += 1
        store.save_job_state(job.name, state)

    except Exception as e:
        logger.exception("Job '%s' failed", job.name)
        state.last_run = datetime.now(UTC).isoformat()
        state.last_result = "error"
        state.last_duration_seconds = round(time.time() - start_time, 1)
        state.last_error = str(e)
        state.run_count += 1
        state.error_count += 1
        store.save_job_state(job.name, state)
    finally:
        if len(messages) > persisted_count:
            store.append_messages(conversation_id, messages[persisted_count:])


class JobRunner:
    """Ticks every 60s, fires jobs whose cron schedule matches."""

    def __init__(
        self,
        config: Config,
        transports: dict[str, Transport],
        store: Store,
        memory_store: MemoryStore | None = None,
    ):
        self._config = config
        self._transports = transports
        self._store = store
        self._memory_store = memory_store
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._tick_loop())
        logger.info("JobRunner started")

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        self._running.clear()
        logger.info("JobRunner stopped")

    async def _tick_loop(self) -> None:
        try:
            while True:
                await self._tick()
                await asyncio.sleep(_seconds_until_next_minute())
        except asyncio.CancelledError:
            return

    async def _tick(self) -> None:
        now = datetime.now(self._config.tz)
        jobs = scan_jobs()

        for job in jobs:
            if not job.enabled or not croniter.match(job.schedule, now):
                continue

            if job.name in self._running:
                logger.debug("Job '%s' still running, skipping", job.name)
                state = self._store.load_job_state(job.name)
                state.skip_count += 1
                self._store.save_job_state(job.name, state)
                continue

            logger.info("Firing job '%s' (schedule: %s)", job.name, job.schedule)
            self._spawn(
                job.name,
                _execute_job(
                    job,
                    self._config,
                    self._transports,
                    self._store,
                    self._memory_store,
                ),
            )

    def _spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        self._running.add(name)

        async def _wrapper():
            try:
                await coro
            except Exception:
                logger.exception("Unhandled error in job '%s'", name)
            finally:
                self._running.discard(name)

        task = asyncio.create_task(_wrapper())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _seconds_until_next_minute() -> float:
    now = time.time()
    return max(0.001, 60.0 - (now % 60.0))


async def run_job_now(
    *,
    name: str,
    config: Config,
    store: Store,
    transports: dict[str, Transport] | None = None,
    memory_store: MemoryStore | None = None,
) -> Job:
    """Run a single job by name outside scheduler ticks.

    Raises:
        ValueError: If the named job does not exist.
    """
    job = next((candidate for candidate in scan_jobs() if candidate.name == name), None)
    if job is None:
        raise ValueError(f"job '{name}' not found")

    await _execute_job(job, config, transports or {}, store, memory_store)
    return job
