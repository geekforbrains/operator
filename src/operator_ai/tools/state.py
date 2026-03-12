"""Agent state tools — file-backed state documents.

State files live at ``agents/<name>/state/<key>.json`` inside the
Operator home directory.  Values are stored as JSON.

Scalar state holds a single value (string, number, or boolean).
List state holds an ordered array of scalar values, managed via
append/pop operations so the agent never rewrites the full list.
"""

from __future__ import annotations

import contextvars
import json
import logging
from pathlib import Path
from typing import Any

from operator_ai.config import OPERATOR_DIR
from operator_ai.tools.registry import tool

logger = logging.getLogger("operator.tools.state")

_context_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_state_context", default=None
)

ScalarValue = str | int | float | bool


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
    return _state_dir(agent_name) / f"{safe_key}.json"


def _read_state(path: Path) -> Any:
    """Read and parse a state file. Returns None if the file doesn't exist."""
    if not path.is_file():
        return None
    text = path.read_text()
    return json.loads(text)


def _write_state(path: Path, data: Any) -> None:
    """Write a value to a state file as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# -- Scalar operations -------------------------------------------------------


@tool(description="Read a state value by key.")
async def get_state(key: str) -> str:
    """Read a state value.

    Args:
        key: The state key.
    """
    agent_name = _get_agent_name()
    path = _state_path(agent_name, key)

    try:
        data = _read_state(path)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("get_state: error reading %s: %s", path, e)
        return f"[error reading state: {e}]"

    if data is None:
        return "[not found]"

    return json.dumps(data, indent=2)


@tool(description="Write a scalar state value (string, number, or boolean).")
async def set_state(key: str, value: ScalarValue) -> str:
    """Write a scalar state value.

    Args:
        key: The state key.
        value: The value to store (string, number, or boolean).
    """
    agent_name = _get_agent_name()
    path = _state_path(agent_name, key)

    # Guard against overwriting list state
    try:
        existing = _read_state(path)
    except (OSError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, list):
        return "[error: key is list state — use append_state/pop_state or delete_state first]"

    try:
        _write_state(path, value)
    except OSError as e:
        logger.warning("set_state: error writing %s: %s", path, e)
        return f"[error writing state: {e}]"

    preview = json.dumps(value) if not isinstance(value, str) else value
    logger.info("set_state: %s = %s (agent=%s)", key, preview[:80], agent_name)
    return f"State saved: {key}"


# -- List operations ----------------------------------------------------------


@tool(
    description=(
        "Append a value to a list state key. "
        "Creates the list if the key doesn't exist. "
        "Use max_items to cap the list length (oldest items dropped)."
    )
)
async def append_state(key: str, value: ScalarValue, max_items: int = 0) -> str:
    """Append a value to a list.

    Args:
        key: The state key.
        value: The value to append (string, number, or boolean).
        max_items: Maximum list length. 0 means unlimited.
    """
    agent_name = _get_agent_name()
    path = _state_path(agent_name, key)

    try:
        existing = _read_state(path)
    except (OSError, json.JSONDecodeError):
        existing = None

    if existing is None:
        items: list = []
    elif isinstance(existing, list):
        items = existing
    else:
        return "[error: key is scalar state — use set_state or delete_state first]"

    items.append(value)

    if max_items > 0 and len(items) > max_items:
        items = items[-max_items:]

    try:
        _write_state(path, items)
    except OSError as e:
        logger.warning("append_state: error writing %s: %s", path, e)
        return f"[error writing state: {e}]"

    preview = json.dumps(value) if not isinstance(value, str) else value
    logger.info("append_state: %s += %s (agent=%s)", key, preview[:80], agent_name)
    return f"Appended to {key} ({len(items)} items)"


@tool(description="Remove and return the oldest item from a list state key.")
async def pop_state(key: str) -> str:
    """Remove and return the oldest item from a list.

    Args:
        key: The state key.
    """
    agent_name = _get_agent_name()
    path = _state_path(agent_name, key)

    try:
        existing = _read_state(path)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("pop_state: error reading %s: %s", path, e)
        return f"[error reading state: {e}]"

    if existing is None:
        return "[not found]"
    if not isinstance(existing, list):
        return "[error: key is scalar state, not a list]"
    if len(existing) == 0:
        return "[empty]"

    item = existing.pop(0)

    try:
        _write_state(path, existing)
    except OSError as e:
        logger.warning("pop_state: error writing %s: %s", path, e)
        return f"[error writing state: {e}]"

    logger.info("pop_state: %s (%d remaining, agent=%s)", key, len(existing), agent_name)
    return json.dumps(item)


# -- Common operations --------------------------------------------------------


@tool(description="List all state keys for the current agent.")
async def list_state() -> str:
    """List all state keys."""
    agent_name = _get_agent_name()
    state_dir = _state_dir(agent_name)
    if not state_dir.is_dir():
        return "No state found."

    files = sorted(state_dir.glob("*.json"))
    if not files:
        return "No state found."

    lines: list[str] = []
    for f in files:
        key = f.stem
        try:
            data = json.loads(f.read_text())
            if isinstance(data, list):
                preview = f"[list, {len(data)} items]"
            elif isinstance(data, str):
                preview = data[:80]
            else:
                preview = json.dumps(data)[:80]
        except (OSError, json.JSONDecodeError):
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
