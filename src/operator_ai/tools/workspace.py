from __future__ import annotations

import contextvars
from pathlib import Path

_workspace_var: contextvars.ContextVar[Path] = contextvars.ContextVar("workspace")
_sandboxed_var: contextvars.ContextVar[bool] = contextvars.ContextVar("sandboxed", default=True)


def set_workspace(path: Path, *, sandboxed: bool = True) -> None:
    _workspace_var.set(path)
    _sandboxed_var.set(sandboxed)


def get_workspace() -> Path:
    return _workspace_var.get()


def is_sandboxed() -> bool:
    return _sandboxed_var.get()
