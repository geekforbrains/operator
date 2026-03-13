from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from importlib import import_module
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

_TOOLS: list[ToolDef] = []
_GROUP_ORDER = (
    "memory",
    "files",
    "messaging",
    "skills",
    "jobs",
    "state",
    "shell",
    "web",
    "users",
    "agents",
)

MAX_OUTPUT = 16_384  # 16 KB — keeps tool results within ~4K tokens


def safe_name(name: str, entity: str) -> str:
    """Validate a user-supplied name for a skill, job, or similar entity."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid {entity} name: {name!r}")
    return name


def format_process_output(
    stdout: bytes, stderr: bytes, returncode: int, max_output: int = MAX_OUTPUT
) -> str:
    """Assemble stdout/stderr/exit-code into a single truncated string."""
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    parts: list[str] = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if returncode != 0:
        parts.append(f"[exit code: {returncode}]")
    result = "\n".join(parts) or "[no output]"
    if len(result) > max_output:
        result = result[:max_output] + "\n[truncated — output exceeded 16KB]"
    return result


class ToolDef:
    def __init__(
        self,
        func: Callable[..., Any],
        description: str,
        permission_group: str | None = None,
        status_label: str | Callable[[dict[str, Any]], str] | None = None,
    ):
        self.func = func
        self.name = func.__name__
        self.description = description
        self.permission_group = permission_group
        self.status_label = status_label
        self.parameters = _build_parameters(func)

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        module = import_module(func.__module__)
        group = getattr(module, "__tool_group__", "")
        if not group:
            raise RuntimeError(f"{func.__module__}.{func.__name__} is missing __tool_group__")
        _TOOLS.append(ToolDef(func, description, group))
        return func

    return decorator


def get_tools(*, grouped: bool = False) -> list[ToolDef] | dict[str, list[ToolDef]]:
    if not grouped:
        return list(_TOOLS)

    groups: dict[str, list[ToolDef]] = {}
    for tool_def in _TOOLS:
        groups.setdefault(tool_def.permission_group or "", []).append(tool_def)

    ordered = {group: groups[group] for group in _GROUP_ORDER if group in groups}
    extras = sorted(group for group in groups if group and group not in ordered)
    for group in extras:
        ordered[group] = groups[group]
    return ordered


# --- schema generation from type hints + docstring ---

_JSON_TYPE_BY_PYTHON_TYPE: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _build_parameters(func: Callable[..., Any]) -> dict[str, Any]:
    hints = get_type_hints(func)
    sig = inspect.signature(func)
    doc_args = _parse_docstring_args(func.__doc__ or "")

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        hint = hints.get(param_name, str)
        prop = _schema_for_annotation(hint)
        if param_name in doc_args:
            prop["description"] = doc_args[param_name]
        properties[param_name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation in _JSON_TYPE_BY_PYTHON_TYPE:
        return {"type": _JSON_TYPE_BY_PYTHON_TYPE[annotation]}

    origin = get_origin(annotation)

    if origin is Literal:
        return _schema_for_literal(get_args(annotation))

    if origin in (list, set):
        item_hint = get_args(annotation)[0] if get_args(annotation) else str
        return {"type": "array", "items": _schema_for_annotation(item_hint)}

    if origin is tuple:
        item_hints = [arg for arg in get_args(annotation) if arg is not Ellipsis]
        items = _schema_for_union(item_hints) if item_hints else {"type": "string"}
        return {"type": "array", "items": items}

    if origin in (Union, UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if not args:
            return {"type": "string"}
        if len(args) == 1:
            return _schema_for_annotation(args[0])
        return _schema_for_union(args)

    return {"type": "string"}


def _schema_for_literal(values: tuple[Any, ...]) -> dict[str, Any]:
    if not values:
        return {"type": "string"}

    value_types = {type(value) for value in values}
    if len(value_types) == 1:
        value_type = next(iter(value_types))
        json_type = _JSON_TYPE_BY_PYTHON_TYPE.get(value_type)
        if json_type is not None:
            return {"type": json_type, "enum": list(values)}

    options = []
    for value in values:
        json_type = _JSON_TYPE_BY_PYTHON_TYPE.get(type(value))
        if json_type is None:
            continue
        options.append({"type": json_type, "enum": [value]})
    if len(options) == 1:
        return options[0]
    if options:
        return {"anyOf": options}
    return {"type": "string"}


def _schema_for_union(args: list[Any]) -> dict[str, Any]:
    options: list[dict[str, Any]] = []
    for arg in args:
        schema = _schema_for_annotation(arg)
        if schema not in options:
            options.append(schema)
    if len(options) == 1:
        return options[0]
    return {"anyOf": options}


def _parse_docstring_args(docstring: str) -> dict[str, str]:
    """Parse 'Args:' section from a Google-style docstring."""
    result: dict[str, str] = {}
    in_args = False
    for line in docstring.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            if stripped == "" or (not line.startswith(" ") and ":" not in stripped):
                break
            m = re.match(r"\s*(\w+)\s*(?:\(.*?\))?\s*:\s*(.*)", line)
            if m:
                result[m.group(1)] = m.group(2).strip()
    return result
