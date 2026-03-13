from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

# Import tools to trigger registration
import operator_ai.tools  # noqa: F401
from operator_ai.config import Config, ConfigError, load_config
from operator_ai.job import JobRunner
from operator_ai.layout import ensure_layout
from operator_ai.log_context import setup_logging
from operator_ai.main.dispatcher import Dispatcher
from operator_ai.main.runtime import RuntimeManager
from operator_ai.memory import MemoryIndex, MemoryStore, reindex_diff
from operator_ai.prompts import load_agent_prompt, load_system_prompt
from operator_ai.store import Store, get_store, reset_store
from operator_ai.tools.web import close_session
from operator_ai.transport.base import Transport
from operator_ai.transport.registry import create_transport, transport_logger_names

logger = logging.getLogger("operator")


def create_transports(config: Config, store: Store) -> list[Transport]:
    transports: list[Transport] = []
    for agent_name, agent_cfg in config.agents.items():
        tc = agent_cfg.transport
        if tc is None:
            continue
        try:
            transport = create_transport(
                type_name=tc.type,
                agent_name=agent_name,
                env=tc.env,
                settings=tc.settings,
                store=store,
            )
            transports.append(transport)
        except ValueError as e:
            logger.warning("Skipping transport for agent '%s': %s", agent_name, e)
    return transports


def _setup_logging(log_dir: Path) -> None:
    setup_logging(
        log_dir=log_dir,
        stderr=os.isatty(2),
        noisy_loggers=("httpx", "httpcore", "litellm", "openai", *transport_logger_names()),
    )


def _acquire_lock(base_dir: Path) -> int:
    """Acquire an exclusive process lock. Returns the fd (keep open for lifetime).

    Raises SystemExit if another instance is already running.
    """
    lock_path = base_dir / "operator.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        logger.error("Another operator process is already running")
        sys.exit(1)
    return fd


def _validate_required_prompts(config: Config) -> None:
    """Fail fast when required authored prompt files are missing or unreadable."""
    load_system_prompt(config.system_prompt_path())
    for agent_name in config.agents:
        load_agent_prompt(config, agent_name)


_SWEEP_INTERVAL = 3600  # seconds — sweep expired memories once per hour


async def _sweep_loop(memory_store: MemoryStore) -> None:
    """Periodically sweep expired memory files to trash."""
    try:
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL)
            try:
                memory_store.sweep_expired()
            except Exception:
                logger.exception("Memory sweep failed")
    except asyncio.CancelledError:
        return


async def async_main() -> None:
    try:
        config = load_config()
    except ConfigError as e:
        raise SystemExit(str(e)) from None

    _setup_logging(config.logs_dir())

    lock_fd = _acquire_lock(config.base_dir)  # held for process lifetime
    transport_tasks: list[asyncio.Task[None]] = []
    stop = asyncio.Event()
    handlers_installed = False
    job_runner: JobRunner | None = None
    sweep_task: asyncio.Task[None] | None = None
    transports: list[Transport] = []
    memory_index: MemoryIndex | None = None
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        stop.set()

    try:
        # Bootstrap directory layout
        ensure_layout(config)
        try:
            _validate_required_prompts(config)
        except (FileNotFoundError, OSError) as e:
            raise SystemExit(str(e)) from None

        if not any(a.transport for a in config.agents.values()):
            logger.error("No transports configured in %s", config.base_dir / "operator.yaml")
            sys.exit(1)

        store = get_store(config.db_dir() / "operator.db")

        # Build the memory index (FTS5 + optional vector)
        embed_fn = None
        embed_dims = 1536
        if config.defaults.embeddings:
            embed_dims = config.defaults.embeddings.dimensions
            embed_model = config.defaults.embeddings.model

            def embed_fn(text: str) -> list[float]:
                import litellm

                resp = litellm.embedding(model=embed_model, input=[text])
                return resp.data[0]["embedding"]

        memory_index = MemoryIndex(
            config.db_dir() / "memory_index.db",
            embed_fn=embed_fn,
            embedding_dimensions=embed_dims,
        )
        memory_store = MemoryStore(base_dir=config.base_dir, index=memory_index)

        # Startup reindex — only changed files
        try:
            reindex_diff(memory_store, memory_index)
        except Exception:
            logger.exception("Startup memory reindex failed (non-fatal)")

        if not store.list_users():
            logger.warning(
                "No users configured. Run: operator user add <username> --role admin slack <YOUR_SLACK_USER_ID>"
            )

        runtimes = RuntimeManager()
        dispatcher = Dispatcher(config, store, runtimes, memory_store=memory_store)
        transports = create_transports(config, store)

        if not transports:
            logger.error("No transports could be started (check env vars)")
            sys.exit(1)

        # Register transports (but don't start yet — start() blocks)
        for transport in transports:
            dispatcher.register_transport(transport)

        # Start job runner and memory sweep.
        job_runner = JobRunner(config, dispatcher.transports, store, memory_store=memory_store)
        job_runner.start()
        sweep_task = asyncio.create_task(_sweep_loop(memory_store))

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
        handlers_installed = True

        # Start transports as background tasks and stop if one exits unexpectedly.
        for transport in transports:
            task = asyncio.create_task(transport.start(dispatcher.handle_message))

            def _on_done(
                done: asyncio.Task[None],
                *,
                transport_name: str = transport.agent_name,
            ) -> None:
                if done.cancelled():
                    return
                exc = done.exception()
                if exc is not None:
                    logger.exception(
                        "Transport '%s' crashed; stopping operator",
                        transport_name,
                        exc_info=exc,
                    )
                    stop.set()
                    return
                logger.error(
                    "Transport '%s' exited unexpectedly; stopping operator", transport_name
                )
                stop.set()

            task.add_done_callback(_on_done)
            transport_tasks.append(task)
            logger.info("Transport starting for agent '%s'", transport.agent_name)

        logger.info(
            "Operator running with %d transport(s). Ctrl+C to stop.",
            len(transports),
        )
        await stop.wait()
    finally:
        logger.info("Shutting down...")
        if handlers_installed:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with suppress(NotImplementedError):
                    loop.remove_signal_handler(sig)

        if sweep_task:
            sweep_task.cancel()
            with suppress(asyncio.CancelledError):
                await sweep_task

        if job_runner:
            await job_runner.stop()

        for task in transport_tasks:
            task.cancel()
        if transport_tasks:
            await asyncio.gather(*transport_tasks, return_exceptions=True)
        for transport in transports:
            await transport.stop()

        await close_session()
        if memory_index is not None:
            memory_index.close()
        reset_store()
        os.close(lock_fd)
