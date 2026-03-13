from __future__ import annotations

import contextvars
from pathlib import Path

_workspace_var: contextvars.ContextVar[Path] = contextvars.ContextVar("workspace")
_sandbox_var: contextvars.ContextVar[bool] = contextvars.ContextVar("sandbox", default=True)


def set_workspace(path: Path, *, sandbox: bool = True) -> None:
    _workspace_var.set(path)
    _sandbox_var.set(sandbox)


def get_workspace() -> Path:
    return _workspace_var.get()


def is_sandboxed() -> bool:
    return _sandbox_var.get()
