from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UserContext:
    username: str
    roles: list[str]
    timezone: str | None = None


_user_var: contextvars.ContextVar[UserContext] = contextvars.ContextVar("user_context")


def set_user_context(ctx: UserContext) -> None:
    _user_var.set(ctx)


def get_user_context() -> UserContext | None:
    """Returns None when not set (e.g., job runs)."""
    try:
        return _user_var.get()
    except LookupError:
        return None


# Skill filter context var — used by skill access tools at runtime
_skill_filter_var: contextvars.ContextVar[Callable[[str], bool] | None] = contextvars.ContextVar(
    "skill_filter", default=None
)


def set_skill_filter(f: Callable[[str], bool] | None) -> None:
    _skill_filter_var.set(f)


def get_skill_filter() -> Callable[[str], bool] | None:
    return _skill_filter_var.get()


# Base directory context var — used by tools that need resolved paths
_base_dir_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "base_dir", default=None
)


def set_base_dir(path: Path) -> None:
    _base_dir_var.set(path)


def get_base_dir() -> Path | None:
    return _base_dir_var.get()
