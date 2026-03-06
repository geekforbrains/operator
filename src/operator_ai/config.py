from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("operator.config")

OPERATOR_DIR = Path.home() / ".operator"
CONFIG_PATH = OPERATOR_DIR / "operator.yaml"

# Expose the resolved base directory as an environment variable so that
# run_shell commands, skill scripts, and job hooks can reference paths
# portably via $OPERATOR_HOME instead of relying on tilde expansion.
os.environ.setdefault("OPERATOR_HOME", str(OPERATOR_DIR))


def _normalize_models(values: Any) -> Any:
    """Allow singular 'model' key as alias for 'models' list."""
    if isinstance(values, dict) and "model" in values and "models" not in values:
        values["models"] = [values.pop("model")]
    return values


class DefaultsConfig(BaseModel):
    models: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=25, gt=0)
    context_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    timezone: str = "UTC"
    env_file: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_models(cls, values: Any) -> Any:
        return _normalize_models(values)

    @model_validator(mode="after")
    def validate_models_non_empty(self) -> DefaultsConfig:
        if not self.models:
            raise ValueError("defaults.models must contain at least one model")
        return self

    @model_validator(mode="after")
    def validate_timezone(self) -> DefaultsConfig:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"Unknown timezone: {self.timezone!r}") from None
        return self


class TransportConfig(BaseModel):
    type: str
    bot_token_env: str | None = None
    app_token_env: str | None = None

    @model_validator(mode="after")
    def validate_transport(self) -> TransportConfig:
        self.type = self.type.strip()
        if self.type != "slack":
            raise ValueError(f"Unsupported transport type: {self.type!r}")
        if not self.bot_token_env:
            raise ValueError("transport.bot_token_env is required for slack transport")
        if not self.app_token_env:
            raise ValueError("transport.app_token_env is required for slack transport")
        return self

    def resolve_env(self, key: str, agent_name: str) -> str:
        env_var = getattr(self, key, None)
        if env_var is None:
            raise ValueError(f"Agent '{agent_name}' transport missing '{key}'")
        value = os.environ.get(env_var)
        if not value:
            raise ValueError(f"Agent '{agent_name}' transport: env var '{env_var}' not set")
        return value


class PermissionsConfig(BaseModel):
    tools: list[str] | Literal["*"] | None = None  # None = no block = full access
    skills: list[str] | Literal["*"] | None = None


class RoleConfig(BaseModel):
    agents: list[str]


class AgentConfig(BaseModel):
    models: list[str] | None = None
    max_iterations: int | None = Field(default=None, gt=0)
    context_ratio: float | None = Field(default=None, gt=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    sandbox: bool = True
    transport: TransportConfig | None = None
    permissions: PermissionsConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_models(cls, values: Any) -> Any:
        return _normalize_models(values)


class HarvesterConfig(BaseModel):
    enabled: bool = False
    schedule: str = ""
    model: str = ""

    @model_validator(mode="after")
    def validate_required_when_enabled(self) -> HarvesterConfig:
        if not self.enabled:
            return self
        missing = []
        if not self.schedule:
            missing.append("schedule")
        if not self.model:
            missing.append("model")
        if missing:
            raise ValueError(
                f"memory.harvester is enabled but missing required fields: {', '.join(missing)}"
            )
        from croniter import croniter

        if not croniter.is_valid(self.schedule):
            raise ValueError(
                f"memory.harvester.schedule is not a valid cron expression: {self.schedule!r}"
            )
        return self


class CleanerConfig(BaseModel):
    enabled: bool = False
    schedule: str = ""
    model: str = ""

    @model_validator(mode="after")
    def validate_required_when_enabled(self) -> CleanerConfig:
        if not self.enabled:
            return self
        missing = []
        if not self.schedule:
            missing.append("schedule")
        if not self.model:
            missing.append("model")
        if missing:
            raise ValueError(
                f"memory.cleaner is enabled but missing required fields: {', '.join(missing)}"
            )
        from croniter import croniter

        if not croniter.is_valid(self.schedule):
            raise ValueError(
                f"memory.cleaner.schedule is not a valid cron expression: {self.schedule!r}"
            )
        return self


class MemoryConfig(BaseModel):
    embed_model: str = ""
    embed_dimensions: int = Field(default=1536, gt=0)
    max_memories: int = Field(default=10000, gt=0)
    inject_top_k: int = Field(default=5, ge=0)
    inject_min_relevance: float = Field(default=0.1, ge=0.0, le=1.0)
    harvester: HarvesterConfig = Field(default_factory=HarvesterConfig)
    cleaner: CleanerConfig = Field(default_factory=CleanerConfig)

    @property
    def enabled(self) -> bool:
        return self.harvester.enabled or self.cleaner.enabled

    @model_validator(mode="after")
    def validate_required_when_enabled(self) -> MemoryConfig:
        if not self.enabled:
            return self
        if not self.embed_model:
            raise ValueError("memory.embed_model is required when harvester or cleaner is enabled")
        return self


class SettingsConfig(BaseModel):
    show_usage: bool = False
    reject_response: Literal["announce", "ignore"] = "ignore"


class Config(BaseModel):
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)

    @model_validator(mode="after")
    def validate_no_admin_role(self) -> Config:
        if "admin" in self.roles:
            raise ValueError("admin is a built-in role and cannot be redefined")
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

    def agent_dir(self, agent_name: str) -> Path:
        return OPERATOR_DIR / "agents" / agent_name

    def agent_workspace(self, agent_name: str) -> Path:
        return self.agent_dir(agent_name) / "workspace"

    def agent_prompt_path(self, agent_name: str) -> Path:
        return self.agent_dir(agent_name) / "AGENT.md"

    @property
    def tz(self) -> ZoneInfo:
        """Return the configured timezone as a ZoneInfo instance."""
        return ZoneInfo(self.defaults.timezone)

    @property
    def shared_dir(self) -> Path:
        return OPERATOR_DIR / "shared"

    def default_agent(self) -> str:
        """Return the first agent name from config, or 'default'."""
        if self.agents:
            return next(iter(self.agents))
        return "operator"

    def agent_sandboxed(self, agent_name: str) -> bool:
        agent = self.agents.get(agent_name)
        if agent is not None:
            return agent.sandbox
        return True

    def agent_tool_filter(self, agent_name: str) -> Callable[[str], bool] | None:
        """Return a predicate that returns True if a tool name is allowed, or None for no filtering."""
        agent = self.agents.get(agent_name)
        if not agent or not agent.permissions:
            return None
        tools = agent.permissions.tools
        if tools is None or tools == "*":
            return None
        allowed = set(tools)
        return lambda name: name in allowed

    def agent_skill_filter(self, agent_name: str) -> Callable[[str], bool] | None:
        """Return a predicate that returns True if a skill name is allowed, or None for no filtering."""
        agent = self.agents.get(agent_name)
        if not agent or not agent.permissions:
            return None
        skills = agent.permissions.skills
        if skills is None or skills == "*":
            return None
        allowed = set(skills)
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


def _load_env_file(env_path: str, *, base_dir: Path | None = None) -> None:
    """Load KEY=VALUE lines from a file into os.environ (overrides existing)."""
    p = Path(env_path).expanduser()
    if not p.is_absolute() and base_dir is not None:
        p = (base_dir / p).resolve()
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            os.environ[key] = value


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        raise SystemExit(
            f'Config not found: {path}\nCreate it with at least:\n  defaults:\n    models:\n      - "openai/gpt-4.1"'
        )
    try:
        with path.open() as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        config = Config(**data)
    except yaml.YAMLError as e:
        raise SystemExit(f"Invalid YAML in {path}: {e}") from e
    except Exception as e:
        raise SystemExit(f"Invalid config in {path}: {e}") from e

    if config.defaults.env_file:
        _load_env_file(config.defaults.env_file, base_dir=path.parent)
    return config
