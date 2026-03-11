"""Agent state tools — file-backed state documents.

State files live at ``agents/<name>/state/<key>.yaml`` inside the
Operator home directory.  Values are stored as YAML.
"""

from __future__ import annotations

import contextvars
import logging
from pathlib import Path
from typing import Any

import yaml

from operator_ai.config import OPERATOR_DIR
from operator_ai.tools.registry import tool

logger = logging.getLogger("operator.tools.state")

_context_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_state_context", default=None
)


def configure(context: dict[str, Any]) -> None:
    _context_var.set(context)


def _get_agent_name() -> str:
    ctx = _context_var.get()
    if ctx is None:
        raise RuntimeError("state tools not configured")
    return ctx.get("agent_name", "")


def _state_dir(agent_name: str) -> Path:
    return OPERATOR_DIR / "agents" / agent_name / "state"


def _state_path(agent_name: str, key: str) -> Path:
    # Sanitize key to prevent path traversal
    safe_key = key.replace("/", "_").replace("\\", "_").replace("..", "_")
    return _state_dir(agent_name) / f"{safe_key}.yaml"


@tool(description="Read a state value by key.")
async def get_state(key: str) -> str:
    """Read a state value.

    Args:
        key: The state key.
    """
    agent_name = _get_agent_name()
    path = _state_path(agent_name, key)
    if not path.is_file():
        return "[not found]"

    try:
        text = path.read_text()
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as e:
        logger.warning("get_state: error reading %s: %s", path, e)
        return f"[error reading state: {e}]"

    if isinstance(data, dict) and "value" in data:
        return str(data["value"])
    return str(data)


@tool(description="Write a state value.")
async def set_state(key: str, value: str) -> str:
    """Write a state value.

    Args:
        key: The state key.
        value: The value to store (string).
    """
    agent_name = _get_agent_name()
    path = _state_path(agent_name, key)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {"value": value}
    try:
        path.write_text(yaml.dump(data, default_flow_style=False))
    except OSError as e:
        logger.warning("set_state: error writing %s: %s", path, e)
        return f"[error writing state: {e}]"

    logger.info("set_state: %s = %s (agent=%s)", key, value[:80], agent_name)
    return f"State saved: {key}"


@tool(description="List all state keys for the current agent.")
async def list_state() -> str:
    """List all state keys."""
    agent_name = _get_agent_name()
    state_dir = _state_dir(agent_name)
    if not state_dir.is_dir():
        return "No state found."

    files = sorted(state_dir.glob("*.yaml"))
    if not files:
        return "No state found."

    lines: list[str] = []
    for f in files:
        key = f.stem
        try:
            data = yaml.safe_load(f.read_text())
            if isinstance(data, dict) and "value" in data:
                preview = str(data["value"])[:80]
            else:
                preview = str(data)[:80]
        except (OSError, yaml.YAMLError):
            preview = "[unreadable]"
        lines.append(f"{key} = {preview}")
    return "\n".join(lines)


@tool(description="Delete a state key.")
async def delete_state(key: str) -> str:
    """Delete a state key.

    Args:
        key: The state key.
    """
    agent_name = _get_agent_name()
    path = _state_path(agent_name, key)
    if not path.is_file():
        return f"State key not found: {key}"

    try:
        path.unlink()
    except OSError as e:
        logger.warning("delete_state: error deleting %s: %s", path, e)
        return f"[error deleting state: {e}]"

    logger.info("delete_state: %s (agent=%s)", key, agent_name)
    return f"State deleted: {key}"
