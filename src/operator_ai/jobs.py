from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from croniter import croniter

from operator_ai.agent_runtime import configure_agent_tool_context
from operator_ai.config import Config
from operator_ai.frontmatter import extract_body, parse_frontmatter
from operator_ai.job_specs import scan_job_specs
from operator_ai.log_context import new_run_id, set_run_context
from operator_ai.memory import MemoryStore
from operator_ai.message_timestamps import attach_message_created_at
from operator_ai.run_prompt import JobEnvelope, build_agent_system_prompt
from operator_ai.store import Store
from operator_ai.transport.base import Transport

logger = logging.getLogger("operator.jobs")


@dataclass
class Job:
    name: str
    description: str
    schedule: str
    prompt: str
    path: Path  # absolute path to the .md file
    agent: str = ""
    model: str = ""
    max_iterations: int = 0
    hooks: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def scan_jobs(jobs_dir: Path | None = None) -> list[Job]:
    """Scan jobs/<name>/JOB.md via scan_job_specs, enrich with prompt/hooks/validation."""
    jobs: list[Job] = []

    specs = scan_job_specs(jobs_dir) if jobs_dir is not None else scan_job_specs()
    for spec in specs:
        if not spec.schedule or not croniter.is_valid(spec.schedule):
            logger.warning("Invalid schedule '%s' in %s, skipping", spec.schedule, spec.path)
            continue

        job_md = Path(spec.path)
        try:
            text = job_md.read_text()
            fm = parse_frontmatter(text)
            body = extract_body(text)

            # Coerce hooks to dict (agents sometimes write [] instead of {})
            hooks = (fm or {}).get("hooks") or {}
            if not isinstance(hooks, dict):
                hooks = {}

            jobs.append(
                Job(
                    name=spec.name,
                    description=spec.description,
                    schedule=spec.schedule,
                    prompt=body,
                    path=job_md,
                    agent=spec.agent,
                    model=spec.model,
                    max_iterations=(fm or {}).get("max_iterations", 0),
                    hooks=hooks,
                    enabled=spec.enabled,
                )
            )
        except Exception as e:
            logger.warning("Failed to parse %s: %s", spec.path, e)

    return jobs


async def _run_hook(
    job: Job,
    hook_name: str,
    agent_name: str = "",
    stdin_data: str = "",
    timeout: int = 30,
    operator_home: Path | None = None,
    operator_db: Path | None = None,
) -> tuple[int, str]:
    """Run a hook script, resolved relative to the job directory."""
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

    job_dir = str(job.path.parent)
    operator_root = operator_home or job.path.parents[2]
    env = {
        **os.environ,
        "JOB_NAME": job.name,
        "JOB_DIR": job_dir,
        "OPERATOR_AGENT": agent_name or job.agent,
        "OPERATOR_HOME": str(operator_root),
        "OPERATOR_DB": str(operator_db or operator_root / "db" / "operator.db"),
    }

    logger.debug("Running %s hook for job '%s': %s", hook_name, job.name, full_path)
    hook_start = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            str(full_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=job_dir,
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
        logger.exception("Hook %s for job '%s' failed", hook_name, job.name)
        return 1, f"[hook error: {e}]"


def _resolve_hook_script_path(job: Job, hook_name: str, script_path: str) -> Path | None:
    """Resolve a hook script path relative to the job directory."""
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

    job_dir = job.path.parent
    try:
        resolved = (job_dir / rel_path).resolve()
        resolved.relative_to(job_dir.resolve())
        return resolved
    except Exception:
        logger.warning(
            "Hook %s path escapes job directory in job '%s': %s",
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

    try:
        # Prerun gate
        prerun_output = ""
        hook_timeout = config.defaults.hook_timeout
        if job.hooks.get("prerun"):
            exit_code, prerun_output = await _run_hook(
                job,
                "prerun",
                agent_name=agent_name,
                timeout=hook_timeout,
                operator_home=config.base_dir,
                operator_db=store.path,
            )
            if exit_code != 0:
                logger.info(
                    "Job '%s' gated by prerun hook (exit %d)%s",
                    job.name,
                    exit_code,
                    f": {prerun_output.strip()}" if prerun_output.strip() else "",
                )
                state.last_run = time.time()
                state.last_result = "gated"
                state.last_duration_seconds = round(time.time() - start_time, 1)
                state.gate_count += 1
                store.save_job_state(job.name, state)
                return

        # Lazy imports to avoid circular dependency:
        # jobs -> agent -> tools/__init__ -> tools/jobs -> jobs
        from operator_ai.agent import run_agent
        from operator_ai.tools import messaging

        transport = transports.get(agent_name)
        run_envelope = JobEnvelope(
            name=job.name,
            description=job.description,
            schedule=job.schedule,
            path=job.path,
            prerun_output=prerun_output,
            transport_prompt=transport.get_prompt_extra() if transport else "",
        )
        system_prompt = build_agent_system_prompt(
            config=config,
            agent_name=agent_name,
            memory_store=memory_store,
            skill_filter=config.agent_skill_filter(agent_name),
            run_envelope=run_envelope,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            attach_message_created_at({"role": "user", "content": job.prompt}),
        ]

        # Configure tools with execution context
        messaging.configure({"transport": transport})
        configure_agent_tool_context(
            agent_name=agent_name,
            base_dir=config.base_dir,
            skill_filter=config.agent_skill_filter(agent_name),
            memory_store=memory_store,
        )

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
            skill_filter=config.agent_skill_filter(agent_name),
            shared_dir=config.shared_dir,
            config=config,
            memory_store=memory_store,
            base_dir=config.base_dir,
            run_envelope=run_envelope,
        )

        # Postrun hook
        if job.hooks.get("postrun"):
            exit_code, postrun_output = await _run_hook(
                job,
                "postrun",
                agent_name=agent_name,
                stdin_data=output,
                timeout=hook_timeout,
                operator_home=config.base_dir,
                operator_db=store.path,
            )
            if exit_code != 0:
                details = f": {postrun_output.strip()}" if postrun_output.strip() else ""
                raise RuntimeError(f"postrun hook exited {exit_code}{details}")

        logger.info("Job '%s' completed in %.1fs", job.name, time.time() - start_time)
        state.last_run = time.time()
        state.last_result = "success"
        state.last_duration_seconds = round(time.time() - start_time, 1)
        state.last_error = ""
        state.run_count += 1
        store.save_job_state(job.name, state)

    except Exception as e:
        logger.exception("Job '%s' failed", job.name)
        state.last_run = time.time()
        state.last_result = "error"
        state.last_duration_seconds = round(time.time() - start_time, 1)
        state.last_error = str(e)
        state.run_count += 1
        state.error_count += 1
        store.save_job_state(job.name, state)


class JobRunner:
    """Ticks every 60s, fires jobs whose cron schedule matches.

    Jobs targeting the same agent are serialized through per-agent FIFO
    queues — different agents run concurrently.
    """

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
        self._agent_queues: dict[str, asyncio.Queue[tuple[Job, bool]]] = {}
        self._agent_workers: dict[str, asyncio.Task] = {}
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

        for task in self._agent_workers.values():
            task.cancel()
        if self._agent_workers:
            await asyncio.gather(*self._agent_workers.values(), return_exceptions=True)
        self._agent_workers.clear()
        self._agent_queues.clear()
        self._running.clear()
        logger.info("JobRunner stopped")

    def _ensure_agent_queue(self, agent_name: str) -> asyncio.Queue[tuple[Job, bool]]:
        """Return the queue for *agent_name*, lazily creating it and its worker."""
        if agent_name not in self._agent_queues:
            queue: asyncio.Queue[tuple[Job, bool]] = asyncio.Queue()
            self._agent_queues[agent_name] = queue
            self._agent_workers[agent_name] = asyncio.create_task(self._agent_worker(agent_name))
        return self._agent_queues[agent_name]

    async def _agent_worker(self, agent_name: str) -> None:
        """Drain the job queue for *agent_name*, running one job at a time."""
        queue = self._agent_queues[agent_name]
        try:
            while True:
                job, was_queued = await queue.get()
                if was_queued:
                    logger.info(
                        "Starting queued job '%s' on agent '%s'",
                        job.name,
                        agent_name,
                    )
                try:
                    await _execute_job(
                        job,
                        self._config,
                        self._transports,
                        self._store,
                        self._memory_store,
                    )
                except Exception:
                    logger.exception("Unhandled error in job '%s'", job.name)
                finally:
                    self._running.discard(job.name)
                    queue.task_done()
        except asyncio.CancelledError:
            return

    async def _tick_loop(self) -> None:
        try:
            while True:
                await self._tick()
                await asyncio.sleep(_seconds_until_next_minute())
        except asyncio.CancelledError:
            return

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        jobs = scan_jobs(self._config.jobs_dir())

        for job in jobs:
            if not job.enabled or not croniter.match(job.schedule, now):
                continue

            if job.name in self._running:
                logger.debug("Job '%s' still running, skipping", job.name)
                state = self._store.load_job_state(job.name)
                state.skip_count += 1
                self._store.save_job_state(job.name, state)
                continue

            agent_name = job.agent or self._config.default_agent()
            queue = self._ensure_agent_queue(agent_name)
            pending = queue.qsize()

            self._running.add(job.name)

            if pending > 0:
                logger.info(
                    "Queuing job '%s' on agent '%s' (%d ahead, schedule: %s)",
                    job.name,
                    agent_name,
                    pending,
                    job.schedule,
                )
            else:
                logger.info("Firing job '%s' (schedule: %s)", job.name, job.schedule)

            queue.put_nowait((job, pending > 0))


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
    job = next((candidate for candidate in scan_jobs(config.jobs_dir()) if candidate.name == name), None)
    if job is None:
        raise ValueError(f"job '{name}' not found")

    await _execute_job(job, config, transports or {}, store, memory_store)
    return job
