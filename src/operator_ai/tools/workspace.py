from __future__ import annotations

import contextvars
from pathlib import Path

_workspace_var: contextvars.ContextVar[Path] = contextvars.ContextVar("workspace")


def set_workspace(path: Path) -> None:
    _workspace_var.set(path)


def get_workspace() -> Path:
    return _workspace_var.get()
