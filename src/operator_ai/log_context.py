from __future__ import annotations

import logging
import logging.handlers
import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    agent: str
    run_id: str = ""
    depth: int = 0

    def __str__(self) -> str:
        if not self.run_id:
            return f"[{self.agent}]"
        depth_suffix = f":d{self.depth}" if self.depth else ""
        return f"[{self.agent}:{self.run_id}{depth_suffix}]"


_run_context: ContextVar[RunContext | None] = ContextVar("_run_context", default=None)


def set_run_context(agent: str, run_id: str = "", depth: int = 0) -> None:
    _run_context.set(RunContext(agent=agent, run_id=run_id, depth=depth))


def get_run_context() -> RunContext | None:
    return _run_context.get()


def new_run_id() -> str:
    return os.urandom(4).hex()


class RunContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _run_context.get()
        record.run_ctx = f"{ctx} " if ctx else ""  # type: ignore[attr-defined]
        return True


def setup_logging(
    *,
    log_dir: Path,
    stderr: bool = True,
    noisy_loggers: tuple[str, ...] = (),
) -> None:
    """Configure operator logging with rotating file handler and optional stderr.

    Args:
        log_dir: Directory for log files.
        stderr: Whether to add a stderr handler.
        noisy_loggers: Logger names to suppress to WARNING level.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "operator.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(run_ctx)s%(message)s", datefmt="%H:%M:%S"
    )
    ctx_filter = RunContextFilter()

    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    fh.addFilter(ctx_filter)

    root = logging.getLogger("operator")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)

    if stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        sh.addFilter(ctx_filter)
        root.addHandler(sh)

    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
