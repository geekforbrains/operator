from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from pathlib import Path

__tool_group__ = "skills"

from operator_ai.config import ConfigError, load_config
from operator_ai.tools.context import get_skill_filter, resolve_dir
from operator_ai.tools.registry import MAX_OUTPUT, format_process_output, safe_name, tool
from operator_ai.tools.workspace import get_workspace
from operator_ai.transport.registry import transport_secret_env_vars

logger = logging.getLogger("operator.tools.skills_access")
_SKILL_SUBDIRS = ("scripts/", "references/", "assets/")
_ENV_REF_RE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
_ALLOWED_OPERATOR_ENV = {"OPERATOR_HOME"}


def _skills_dir() -> Path:
    return resolve_dir("skills")


def _expand_env_refs(value: str, env: dict[str, str]) -> str:
    """Expand $VAR and ${VAR} placeholders against the provided env mapping."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        return env.get(key, match.group(0))

    return _ENV_REF_RE.sub(replace, value)


def _check_skill_access(skill: str) -> str | None:
    """Check if skill is accessible. Returns error string or None."""
    try:
        safe_name(skill, "skill")
    except ValueError:
        return f"[error: invalid skill name: {skill!r}]"

    skill_filter = get_skill_filter()
    if skill_filter is not None and not skill_filter(skill):
        return f"[error: skill '{skill}' is not available to this agent]"

    skill_dir = _skills_dir() / skill
    if not skill_dir.is_dir():
        return f"[error: skill '{skill}' not found]"

    return None


@tool(
    description=(
        "Read skill content (SKILL.md, references, assets). "
        "Use to understand a skill before running it."
    ),
)
async def read_skill(skill: str, path: str = "") -> str:
    """Read skill content.

    Args:
        skill: Skill name.
        path: Relative path within the skill directory. Empty = SKILL.md.
    """
    err = _check_skill_access(skill)
    if err:
        return err

    skill_dir = _skills_dir() / skill

    if not path:
        target = skill_dir / "SKILL.md"
    else:
        # Block path traversal
        if ".." in path.split("/") or ".." in path.split("\\"):
            return "[error: path traversal not allowed]"
        target = skill_dir / path

    if not target.is_file():
        return f"[error: file not found: {target}]"

    try:
        content = target.read_text()
    except Exception as e:
        return f"[error reading file: {e}]"

    if len(content) > MAX_OUTPUT:
        content = content[:MAX_OUTPUT] + "\n[truncated — output exceeded 16KB]"
    return content


@tool(
    description=(
        "Execute a command in the context of a skill. The command runs with "
        "shell=False (no pipes, redirects, or chaining). Skill scripts/ paths "
        "and env var references are auto-expanded."
    ),
)
async def run_skill(skill: str, command: str, timeout: int = 120) -> str:
    """Execute a command in a skill's context.

    Args:
        skill: Skill name.
        command: Command and arguments as a string (parsed with shlex.split).
        timeout: Timeout in seconds (default 120).
    """
    err = _check_skill_access(skill)
    if err:
        return err

    skill_dir = _skills_dir() / skill
    if not skill_dir.is_dir():
        return f"[error: skill '{skill}' not found]"

    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"[error: invalid command: {e}]"

    if not argv:
        return "[error: empty command]"

    # Build env — os.environ already has the correct PATH from .env loading
    env = os.environ.copy()
    env["SKILL_DIR"] = str(skill_dir)

    # Strip transport-config env vars
    try:
        config = load_config()
        strip_keys: set[str] = set()
        for agent_cfg in config.agents.values():
            tc = agent_cfg.transport
            if tc is None:
                continue
            strip_keys.update(transport_secret_env_vars(tc.type, tc.env, tc.settings))
        for key in strip_keys:
            env.pop(key, None)
    except ConfigError:
        # Config might not be loadable in test environments
        pass

    # Strip any OPERATOR_* vars
    for key in list(env.keys()):
        if key.startswith("OPERATOR_") and key not in _ALLOWED_OPERATOR_ENV:
            del env[key]

    # Expand env references after sanitization so stripped vars stay unavailable.
    argv = [_expand_env_refs(arg, env) for arg in argv]

    # Path expansion: expand skill subdirectory references to absolute paths
    for i in range(len(argv)):
        for prefix in _SKILL_SUBDIRS:
            if argv[i].startswith(prefix):
                argv[i] = str(skill_dir / argv[i])
                break

    proc: asyncio.subprocess.Process | None = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=get_workspace(),
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise
    except FileNotFoundError:
        return f"[error: command not found: {argv[0]}]"
    except OSError as e:
        return f"[error: {e}]"
    except TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        return f"[timed out after {timeout}s]"

    return format_process_output(stdout, stderr, proc.returncode)
