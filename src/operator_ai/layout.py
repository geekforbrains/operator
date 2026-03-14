"""Directory layout bootstrap.

``ensure_layout(config)`` creates the full ``~/.operator/`` tree at startup.
All operations are idempotent — running it twice produces no errors and no
duplicate work.
"""

from __future__ import annotations

import logging
from pathlib import Path

from operator_ai.config import Config, ensure_shared_symlink

logger = logging.getLogger("operator.layout")

# Fixed subdirectories inside every agent workspace.
_WORKSPACE_SUBDIRS = ("inbox", "work", "artifacts", "tmp")

# Fixed subdirectories inside every memory scope (agent, global, user).
_MEMORY_SUBDIRS = ("rules", "notes", "trash")


def _ensure_dirs(*paths: Path) -> None:
    """Create directories (with parents) if they don't already exist."""
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _ensure_agent(name: str, config: Config) -> None:
    """Bootstrap a single agent's directory tree."""
    agent_dir = config.agent_dir(name)

    # workspace/<subdir>
    ws = config.agent_workspace(name)
    _ensure_dirs(*(ws / sub for sub in _WORKSPACE_SUBDIRS))

    # workspace/shared symlink → ~/.operator/shared/
    ensure_shared_symlink(ws, config.shared_dir)

    # memory/{rules,notes,trash}
    mem = config.agent_memory_dir(name)
    _ensure_dirs(*(mem / sub for sub in _MEMORY_SUBDIRS))

    # state/
    _ensure_dirs(config.agent_state_dir(name))

    logger.debug("layout: agent %s ready at %s", name, agent_dir)


def ensure_layout(config: Config) -> None:
    """Create the full ``~/.operator/`` directory tree.

    This is safe to call on every startup. It creates directories, symlinks,
    and other runtime-managed layout state. Authored prompt files are created
    by ``operator init`` and are not synthesized at runtime.

    Parameters
    ----------
    config:
        A loaded :class:`Config` whose ``agents`` dict determines which
        per-agent subtrees are created.
    """
    home = config.base_dir

    # Top-level directories
    _ensure_dirs(
        home,
        config.jobs_dir(),
        config.skills_dir(),
        config.shared_dir,
        config.db_dir(),
        config.logs_dir(),
    )

    # Global memory
    gm = config.global_memory_dir()
    _ensure_dirs(*(gm / sub for sub in _MEMORY_SUBDIRS))

    # User memory root (populated per known user, but ensure the parent)
    _ensure_dirs(home / "memory" / "users")

    # Per-agent trees
    for name in config.agents:
        _ensure_agent(name, config)

    logger.info("layout: directory tree ready under %s", home)


def ensure_user_memory(username: str, config: Config) -> None:
    """Create the memory subtree for a single user.

    Called when a user is first seen so their memory directories exist.
    """
    um = config.user_memory_dir(username)
    _ensure_dirs(*(um / sub for sub in _MEMORY_SUBDIRS))
    logger.debug("layout: user memory ready for %s", username)
