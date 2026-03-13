from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

logger = logging.getLogger("operator.config")


class ConfigError(Exception):
    """Raised when configuration is missing, unreadable, or invalid."""


OPERATOR_DIR = Path.home() / ".operator"
CONFIG_PATH = OPERATOR_DIR / "operator.yaml"
SKILLS_DIR = OPERATOR_DIR / "skills"
LOGS_DIR = OPERATOR_DIR / "logs"

# Expose the resolved base directory as an environment variable so that
# run_shell commands, skill scripts, and job hooks can reference paths
# portably via $OPERATOR_HOME instead of relying on tilde expansion.
os.environ.setdefault("OPERATOR_HOME", str(OPERATOR_DIR))

ThinkingLevel = Literal["off", "low", "medium", "high"]


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"Unknown timezone: {value!r}") from None
    return value


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmbeddingConfig(StrictConfigModel):
    model: str  # e.g. "openai/text-embedding-3-small"
    dimensions: int = Field(default=1536, gt=0)


class DefaultsConfig(StrictConfigModel):
    models: list[str] = Field(default_factory=list)
    thinking: ThinkingLevel = "off"
    max_iterations: int = Field(default=25, gt=0)
    context_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    hook_timeout: int = Field(default=30, gt=0)
    embeddings: EmbeddingConfig | None = None

    @model_validator(mode="after")
    def validate_models_non_empty(self) -> DefaultsConfig:
        if not self.models:
            raise ValueError("defaults.models must contain at least one model")
        return self


class RuntimeConfig(StrictConfigModel):
    env_file: str | None = None
    show_usage: bool = False
    reject_response: Literal["announce", "ignore"] = "ignore"


class TransportConfig(StrictConfigModel):
    type: str
    env: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_transport(self) -> TransportConfig:
        from operator_ai.transport.registry import normalize_transport_config

        self.type = self.type.strip().lower()
        self.env, self.settings = normalize_transport_config(
            self.type,
            self.env,
            self.settings,
        )
        return self


class PermissionsConfig(StrictConfigModel):
    # None = deny all (closed by default), "*" = allow all, list = allowlist
    tools: list[str] | Literal["*"] | None = None
    skills: list[str] | Literal["*"] | None = None


class RoleConfig(StrictConfigModel):
    agents: list[str]


class AgentConfig(StrictConfigModel):
    models: list[str] | None = None
    thinking: ThinkingLevel | None = None
    max_iterations: int | None = Field(default=None, gt=0)
    context_ratio: float | None = Field(default=None, gt=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    sandbox: bool = True
    transport: TransportConfig | None = None
    permissions: PermissionsConfig | None = None


class Config(StrictConfigModel):
    _base_dir: Path | None = PrivateAttr(default=None)

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    permission_groups: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_no_admin_role(self) -> Config:
        if "admin" in self.roles:
            raise ValueError("admin is a built-in role and cannot be redefined")
        return self

    @property
    def base_dir(self) -> Path:
        return self._base_dir or OPERATOR_DIR

    def set_base_dir(self, base_dir: Path) -> Config:
        self._base_dir = base_dir.expanduser().resolve()
        os.environ["OPERATOR_HOME"] = str(self._base_dir)
        return self

    def agent_models(self, agent_name: str) -> list[str]:
        agent = self.agents.get(agent_name)
        if agent and agent.models:
            return agent.models
        return self.defaults.models

    def agent_max_iterations(self, agent_name: str) -> int:
        agent = self.agents.get(agent_name)
        if agent and agent.max_iterations is not None:
            return agent.max_iterations
        return self.defaults.max_iterations

    def agent_thinking(self, agent_name: str) -> ThinkingLevel:
        agent = self.agents.get(agent_name)
        if agent and agent.thinking is not None:
            return agent.thinking
        return self.defaults.thinking

    def agent_context_ratio(self, agent_name: str) -> float:
        agent = self.agents.get(agent_name)
        if agent and agent.context_ratio is not None:
            return agent.context_ratio
        return self.defaults.context_ratio

    def agent_max_output_tokens(self, agent_name: str) -> int | None:
        agent = self.agents.get(agent_name)
        if agent and agent.max_output_tokens is not None:
            return agent.max_output_tokens
        return self.defaults.max_output_tokens

    def agent_sandbox(self, agent_name: str) -> bool:
        agent = self.agents.get(agent_name)
        if agent is not None:
            return agent.sandbox
        return True

    def agent_dir(self, agent_name: str) -> Path:
        return self.base_dir / "agents" / agent_name

    def agent_workspace(self, agent_name: str) -> Path:
        return self.agent_dir(agent_name) / "workspace"

    def agent_prompt_path(self, agent_name: str) -> Path:
        return self.agent_dir(agent_name) / "AGENT.md"

    def agent_memory_dir(self, name: str) -> Path:
        return self.agent_dir(name) / "memory"

    def agent_state_dir(self, name: str) -> Path:
        return self.agent_dir(name) / "state"

    def global_memory_dir(self) -> Path:
        return self.base_dir / "memory" / "global"

    def user_memory_dir(self, username: str) -> Path:
        return self.base_dir / "memory" / "users" / username

    def system_prompt_path(self) -> Path:
        return self.base_dir / "SYSTEM.md"

    def jobs_dir(self) -> Path:
        return self.base_dir / "jobs"

    def skills_dir(self) -> Path:
        return self.base_dir / "skills"

    @property
    def shared_dir(self) -> Path:
        return self.base_dir / "shared"

    def db_dir(self) -> Path:
        return self.base_dir / "db"

    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    def default_agent(self) -> str:
        """Return the first agent name from config, or 'default'."""
        if self.agents:
            return next(iter(self.agents))
        return "operator"

    def _expand_permission_list(self, items: list[str], kind: str = "tools") -> set[str]:
        """Expand @group references in a permission list."""
        expanded: set[str] = set()
        for item in items:
            if item.startswith("@"):
                group_name = item[1:]
                group_tools = self.permission_groups.get(group_name)
                if group_tools is None:
                    logger.warning("Unknown permission group '@%s' in %s", group_name, kind)
                else:
                    expanded.update(group_tools)
            else:
                expanded.add(item)
        return expanded

    def agent_tool_filter(self, agent_name: str) -> Callable[[str], bool]:
        """Return a predicate that returns True if a tool name is allowed.

        Closed-by-default: no agent config, no permissions block, or
        tools=None means nothing is allowed.  '*' means everything is allowed.
        """
        agent = self.agents.get(agent_name)
        if not agent or not agent.permissions:
            return lambda _name: False
        tools = agent.permissions.tools
        if tools is None:
            return lambda _name: False
        if tools == "*":
            return lambda _name: True
        allowed = self._expand_permission_list(list(tools), kind="tools")
        return lambda name: name in allowed

    def agent_skill_filter(self, agent_name: str) -> Callable[[str], bool]:
        """Return a predicate that returns True if a skill name is allowed.

        Closed-by-default: no agent config, no permissions block, or
        skills=None means nothing is allowed.  '*' means everything is allowed.
        """
        agent = self.agents.get(agent_name)
        if not agent or not agent.permissions:
            return lambda _name: False
        skills = agent.permissions.skills
        if skills is None:
            return lambda _name: False
        if skills == "*":
            return lambda _name: True
        allowed = self._expand_permission_list(list(skills), kind="skills")
        return lambda name: name in allowed


def ensure_shared_symlink(workspace: Path, shared: Path) -> None:
    """Ensure workspace/shared is a symlink to the shared directory."""
    shared.mkdir(parents=True, exist_ok=True)
    link = workspace / "shared"
    if link.is_symlink():
        if link.resolve() == shared.resolve():
            return
        link.unlink()
    elif link.exists():
        # Not a symlink but something else exists — don't clobber
        logger.warning("shared: %s exists and is not a symlink, skipping", link)
        return
    link.symlink_to(shared)
    logger.info("shared: created symlink %s → %s", link, shared)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a dotenv file. Returns a dict of key-value pairs."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _load_env_file(env_path: str, *, base_dir: Path | None = None) -> None:
    """Load KEY=VALUE lines from a file into os.environ (doesn't override existing)."""
    p = Path(env_path).expanduser()
    if not p.is_absolute() and base_dir is not None:
        p = (base_dir / p).resolve()
    for key, value in parse_env_file(p).items():
        os.environ.setdefault(key, value)


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f'Config not found: {path}\nCreate it with at least:\n  defaults:\n    models:\n      - "openai/gpt-4.1"'
        )
    try:
        with path.open() as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        config = Config(**data)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    except Exception as e:
        raise ConfigError(f"Invalid config in {path}: {e}") from e

    config.set_base_dir(path.parent)

    if config.runtime.env_file:
        _load_env_file(config.runtime.env_file, base_dir=path.parent)
    return config
