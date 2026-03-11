from __future__ import annotations

import asyncio
from pathlib import Path

from operator_ai.tools.registry import MAX_OUTPUT, tool
from operator_ai.tools.workspace import get_workspace

MAX_READ_BYTES = 1_000_000  # 1 MB


def _resolve(path: str) -> Path:
    """Resolve a path relative to the agent workspace.

    Allows absolute paths and paths outside the workspace.
    """
    workspace = get_workspace().resolve()
    p = Path(path).expanduser()
    return p.resolve() if p.is_absolute() else (workspace / p).resolve()


@tool(description="Read the contents of a file.")
async def read_file(path: str) -> str:
    """Read a file.

    Args:
        path: File path (relative to workspace, or absolute).
    """
    try:
        p = _resolve(path)
    except ValueError as e:
        return f"[error: {e}]"

    def _read_sync() -> str:
        if not p.exists():
            return f"[error: file not found: {path}]"
        try:
            size = p.stat().st_size
            with p.open("rb") as f:
                data = f.read(MAX_READ_BYTES)
            text = data.decode(errors="replace")
            if len(text) > MAX_OUTPUT:
                text = (
                    text[:MAX_OUTPUT]
                    + f"\n[truncated — output exceeded 16KB, file is {size} bytes]"
                )
            elif size > MAX_READ_BYTES:
                text += f"\n[truncated at {MAX_READ_BYTES} bytes, file is {size} bytes]"
            return text
        except Exception as e:
            return f"[error reading file: {e}]"

    return await asyncio.to_thread(_read_sync)


@tool(description="Write content to a file. Creates parent directories if needed.")
async def write_file(path: str, content: str) -> str:
    """Write a file.

    Args:
        path: File path (relative to workspace, or absolute).
        content: The content to write.
    """
    try:
        p = _resolve(path)
    except ValueError as e:
        return f"[error: {e}]"

    def _write_sync() -> str:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"Wrote {len(content)} bytes to {p}"
        except Exception as e:
            return f"[error writing file: {e}]"

    return await asyncio.to_thread(_write_sync)


@tool(description="List files and directories at the given path.")
async def list_files(path: str = ".", max_depth: int = 2) -> str:
    """List directory contents.

    Args:
        path: Directory path to list (default: current directory).
        max_depth: Maximum depth to recurse (default: 2).
    """
    try:
        root = _resolve(path)
    except ValueError as e:
        return f"[error: {e}]"
    if not root.is_dir():
        return f"[error: not a directory: {path}]"

    def _walk_sync() -> list[str]:
        lines: list[str] = []

        def _walk(p: Path, depth: int, prefix: str = "") -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
            except PermissionError:
                lines.append(f"{prefix}[permission denied]")
                return
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    lines.append(f"{prefix}{name}/")
                    _walk(entry, depth + 1, prefix + "  ")
                else:
                    lines.append(f"{prefix}{name}")

        _walk(root, 1)
        return lines

    lines = await asyncio.to_thread(_walk_sync)
    return "\n".join(lines) if lines else "[empty directory]"
