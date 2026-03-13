from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from operator_ai.config import OPERATOR_DIR, Config
from operator_ai.memory import MemoryStore


def resolve_base_dir(*, config: Config | None = None, base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir
    if config is not None:
        return config.base_dir
    home = os.environ.get("OPERATOR_HOME", str(OPERATOR_DIR))
    return Path(home).expanduser().resolve()


def configure_agent_tool_context(
    *,
    agent_name: str,
    base_dir: Path,
    skill_filter: Callable[[str], bool] | None,
    memory_store: MemoryStore | None,
    username: str = "",
    allow_user_scope: bool = False,
) -> None:
    from operator_ai.tools import memory as memory_tools
    from operator_ai.tools import state as state_tools
    from operator_ai.tools.context import set_base_dir, set_skill_filter

    set_base_dir(base_dir)
    set_skill_filter(skill_filter)
    state_tools.configure(
        {
            "agent_name": agent_name,
            "base_dir": base_dir,
        }
    )
    memory_tools.configure(
        {
            "memory_store": memory_store,
            "username": username,
            "agent_name": agent_name,
            "allow_user_scope": allow_user_scope,
        }
    )
