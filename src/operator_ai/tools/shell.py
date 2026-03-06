from __future__ import annotations

import asyncio

from operator_ai.tools.registry import format_process_output, tool
from operator_ai.tools.workspace import get_workspace


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

    return format_process_output(stdout, stderr, proc.returncode)
