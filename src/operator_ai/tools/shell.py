from __future__ import annotations

import asyncio

from operator_ai.tools.registry import tool
from operator_ai.tools.workspace import get_workspace

_MAX_OUTPUT = 16_384  # 16 KB — keeps tool results within ~4K tokens


@tool(
    description="Execute a shell command and return its output. Use for system commands, package management, git, etc.",
)
async def run_shell(command: str, timeout: int = 120) -> str:
    """Run a shell command.

    Args:
        command: The shell command to execute.
        timeout: Timeout in seconds (default 120).
    """
    proc: asyncio.subprocess.Process | None = None

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=get_workspace(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        # !stop should terminate in-flight shell commands immediately.
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise
    except TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        return f"[timed out after {timeout}s]"

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    parts: list[str] = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if proc.returncode != 0:
        parts.append(f"[exit code: {proc.returncode}]")
    result = "\n".join(parts) or "[no output]"
    if len(result) > _MAX_OUTPUT:
        result = result[:_MAX_OUTPUT] + "\n[truncated — output exceeded 16KB]"
    return result
