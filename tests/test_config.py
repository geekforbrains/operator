from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from operator_ai.config import (
    OPERATOR_DIR,
    Config,
    ConfigError,
    PermissionsConfig,
    RoleConfig,
    RuntimeConfig,
    _load_env_file,
    ensure_shared_symlink,
    load_config,
)

# ── Helpers ─────────────────────────────────────────────────────


def _cfg(**agent_kwargs: object) -> Config:
    return Config(defaults={"models": ["test/m"]}, agents={"a": agent_kwargs})


def _min_cfg(**overrides: object) -> Config:
    data: dict[str, object] = {"defaults": {"models": ["test/m"]}}
    data.update(overrides)
    return Config(**data)


# ── Basic config parsing ────────────────────────────────────────


def test_minimal_config() -> None:
    c = _min_cfg()
    assert c.defaults.models == ["test/m"]
    assert c.defaults.thinking == "off"
    assert c.defaults.max_iterations == 25
    assert c.runtime.timezone == "UTC"


def test_defaults_require_at_least_one_model() -> None:
    with pytest.raises(ValueError, match=r"defaults\.models must contain at least one model"):
        Config(defaults={"models": []})


def test_singular_model_alias() -> None:
    c = Config(defaults={"model": "test/m"})
    assert c.defaults.models == ["test/m"]


def test_agent_model_override() -> None:
    c = Config(
        defaults={"models": ["default/m"]},
        agents={"a": {"models": ["agent/m"]}},
    )
    assert c.agent_models("a") == ["agent/m"]
    assert c.agent_models("unknown") == ["default/m"]


def test_agent_singular_model_alias() -> None:
    c = Config(
        defaults={"models": ["default/m"]},
        agents={"a": {"model": "agent/m"}},
    )
    assert c.agent_models("a") == ["agent/m"]


# ── Timezone ────────────────────────────────────────────────────


def test_timezone_defaults_to_utc() -> None:
    runtime = RuntimeConfig()
    assert runtime.timezone == "UTC"


def test_timezone_override() -> None:
    runtime = RuntimeConfig(timezone="America/Vancouver")
    assert runtime.timezone == "America/Vancouver"


def test_config_tz_returns_zoneinfo() -> None:
    c = Config(defaults={"models": ["test/m"]}, runtime={"timezone": "Europe/London"})
    assert c.tz == ZoneInfo("Europe/London")


def test_config_tz_defaults_to_utc() -> None:
    c = _min_cfg()
    assert c.tz == ZoneInfo("UTC")


def test_invalid_timezone_raises() -> None:
    with pytest.raises(ValueError, match="Unknown timezone"):
        RuntimeConfig(timezone="Mars/Olympus")


# ── Thinking ────────────────────────────────────────────────────


def test_agent_thinking_defaults_to_off() -> None:
    c = _min_cfg()
    assert c.agent_thinking("operator") == "off"


def test_agent_thinking_agent_override_wins() -> None:
    c = Config(
        defaults={"models": ["test/m"], "thinking": "low"},
        agents={"operator": {"thinking": "high"}},
    )
    assert c.agent_thinking("operator") == "high"
    assert c.agent_thinking("other") == "low"


def test_invalid_thinking_level_raises() -> None:
    with pytest.raises(ValueError, match="thinking"):
        Config(defaults={"models": ["test/m"], "thinking": "max"})


# ── Context ratio / max iterations / max output tokens ──────────


def test_agent_context_ratio_fallback() -> None:
    c = _min_cfg()
    assert c.agent_context_ratio("a") == 0.5


def test_agent_context_ratio_override() -> None:
    c = _cfg(context_ratio=0.8)
    assert c.agent_context_ratio("a") == 0.8


def test_agent_max_iterations_fallback() -> None:
    c = _min_cfg()
    assert c.agent_max_iterations("a") == 25


def test_agent_max_iterations_override() -> None:
    c = _cfg(max_iterations=10)
    assert c.agent_max_iterations("a") == 10


def test_agent_max_output_tokens_fallback() -> None:
    c = _min_cfg()
    assert c.agent_max_output_tokens("a") is None


def test_agent_max_output_tokens_override() -> None:
    c = _cfg(max_output_tokens=4096)
    assert c.agent_max_output_tokens("a") == 4096


# ── Permissions (closed-by-default) ─────────────────────────────


def test_permissions_defaults_to_empty_lists() -> None:
    p = PermissionsConfig()
    assert p.tools == []
    assert p.skills == []


def test_permissions_star_allows_all() -> None:
    p = PermissionsConfig(tools="*", skills="*")
    assert p.tools == "*"
    assert p.skills == "*"


def test_permissions_explicit_list() -> None:
    p = PermissionsConfig(tools=["a", "b"], skills=["c"])
    assert p.tools == ["a", "b"]
    assert p.skills == ["c"]


def test_no_agent_config_denies_all_tools() -> None:
    """An unknown agent has no config — everything denied."""
    c = _min_cfg()
    f = c.agent_tool_filter("nonexistent")
    assert f("anything") is False


def test_no_permissions_block_denies_all() -> None:
    """Agent exists but has no permissions block — everything denied."""
    c = _cfg()  # no permissions kwarg
    assert c.agent_tool_filter("a")("read_file") is False
    assert c.agent_skill_filter("a")("deploy") is False


def test_empty_permissions_denies_all() -> None:
    """Agent has an empty permissions block — defaults are empty lists."""
    c = _cfg(permissions={})
    assert c.agent_tool_filter("a")("read_file") is False
    assert c.agent_skill_filter("a")("deploy") is False


def test_star_permissions_allows_everything() -> None:
    c = _cfg(permissions={"tools": "*", "skills": "*"})
    assert c.agent_tool_filter("a")("anything") is True
    assert c.agent_skill_filter("a")("anything") is True


def test_tool_list_filter() -> None:
    c = _cfg(permissions={"tools": ["read_file", "list_files"]})
    f = c.agent_tool_filter("a")
    assert f("read_file") is True
    assert f("run_shell") is False


def test_skill_list_filter() -> None:
    c = _cfg(permissions={"skills": ["deploy"]})
    f = c.agent_skill_filter("a")
    assert f("deploy") is True
    assert f("other") is False


# ── Roles ───────────────────────────────────────────────────────


def test_role_config_validation() -> None:
    r = RoleConfig(agents=["alice", "bob"])
    assert r.agents == ["alice", "bob"]


def test_admin_role_raises() -> None:
    with pytest.raises(ValueError, match="admin is a built-in role"):
        Config(
            defaults={"models": ["test/m"]},
            roles={"admin": {"agents": ["alice"]}},
        )


def test_custom_roles_allowed() -> None:
    c = Config(
        defaults={"models": ["test/m"]},
        roles={"developer": {"agents": ["alice"]}},
    )
    assert "developer" in c.roles
    assert c.roles["developer"].agents == ["alice"]


# ── RuntimeConfig ────────────────────────────────────────────────


def test_runtime_defaults() -> None:
    runtime = RuntimeConfig()
    assert runtime.show_usage is False
    assert runtime.reject_response == "ignore"


def test_runtime_reject_response_announce() -> None:
    runtime = RuntimeConfig(reject_response="announce")
    assert runtime.reject_response == "announce"


# ── Extra fields rejected (strict) ──────────────────────────────


def test_legacy_defaults_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Config(defaults={"models": ["test/m"], "timezone": "Europe/London"})


def test_legacy_settings_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="settings"):
        Config(defaults={"models": ["test/m"]}, settings={"reject_response": "announce"})


def test_memory_block_is_rejected() -> None:
    """The old memory block no longer exists — strict mode rejects it."""
    with pytest.raises(ValueError, match="memory"):
        Config(defaults={"models": ["test/m"]}, memory={"embed_model": "x"})


# ── Path helpers ────────────────────────────────────────────────


def test_agent_dir() -> None:
    c = _min_cfg()
    assert c.agent_dir("hermy") == OPERATOR_DIR / "agents" / "hermy"


def test_agent_workspace() -> None:
    c = _min_cfg()
    assert c.agent_workspace("hermy") == OPERATOR_DIR / "agents" / "hermy" / "workspace"


def test_agent_prompt_path() -> None:
    c = _min_cfg()
    assert c.agent_prompt_path("hermy") == OPERATOR_DIR / "agents" / "hermy" / "AGENT.md"


def test_agent_memory_dir() -> None:
    c = _min_cfg()
    assert c.agent_memory_dir("hermy") == OPERATOR_DIR / "agents" / "hermy" / "memory"


def test_agent_state_dir() -> None:
    c = _min_cfg()
    assert c.agent_state_dir("hermy") == OPERATOR_DIR / "agents" / "hermy" / "state"


def test_global_memory_dir() -> None:
    c = _min_cfg()
    assert c.global_memory_dir() == OPERATOR_DIR / "memory" / "global"


def test_user_memory_dir() -> None:
    c = _min_cfg()
    assert c.user_memory_dir("gavin") == OPERATOR_DIR / "memory" / "users" / "gavin"


def test_system_prompt_path() -> None:
    c = _min_cfg()
    assert c.system_prompt_path() == OPERATOR_DIR / "SYSTEM.md"


def test_jobs_dir() -> None:
    c = _min_cfg()
    assert c.jobs_dir() == OPERATOR_DIR / "jobs"


def test_skills_dir() -> None:
    c = _min_cfg()
    assert c.skills_dir() == OPERATOR_DIR / "skills"


def test_shared_dir() -> None:
    c = _min_cfg()
    assert c.shared_dir == OPERATOR_DIR / "shared"


def test_db_dir() -> None:
    c = _min_cfg()
    assert c.db_dir() == OPERATOR_DIR / "db"


def test_default_agent_first_in_dict() -> None:
    c = Config(
        defaults={"models": ["test/m"]},
        agents={"hermy": {}, "cora": {}},
    )
    assert c.default_agent() == "hermy"


def test_default_agent_fallback() -> None:
    c = _min_cfg()
    assert c.default_agent() == "operator"


# ── Shared symlink ──────────────────────────────────────────────


def test_ensure_shared_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"

    ensure_shared_symlink(workspace, shared)

    link = workspace / "shared"
    assert link.is_symlink()
    assert link.resolve() == shared.resolve()
    assert shared.is_dir()


def test_ensure_shared_symlink_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"

    ensure_shared_symlink(workspace, shared)
    ensure_shared_symlink(workspace, shared)

    assert (workspace / "shared").is_symlink()


def test_ensure_shared_symlink_skips_non_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"
    (workspace / "shared").mkdir()

    ensure_shared_symlink(workspace, shared)

    assert not (workspace / "shared").is_symlink()
    assert (workspace / "shared").is_dir()


# ── _load_env_file ──────────────────────────────────────────────


def test_load_env_file_does_not_override_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_VAR=from_file\n")
    monkeypatch.setenv("MY_TEST_VAR", "original")

    _load_env_file(str(env_file))

    assert os.environ["MY_TEST_VAR"] == "original"


def test_load_env_file_strips_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUOTED_VAR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('QUOTED_VAR="hello world"\n')

    _load_env_file(str(env_file))

    assert os.environ["QUOTED_VAR"] == "hello world"


def test_load_env_file_skips_comments_and_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VALID_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nVALID_KEY=yes\n")

    _load_env_file(str(env_file))

    assert os.environ["VALID_KEY"] == "yes"


def test_load_env_file_missing_file_is_noop(tmp_path: Path) -> None:
    _load_env_file(str(tmp_path / "nonexistent.env"))


def test_load_config_reads_runtime_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPERATOR_RUNTIME_ENV_TEST", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPERATOR_RUNTIME_ENV_TEST=loaded\n")
    config_path = tmp_path / "operator.yaml"
    config_path.write_text(
        'runtime:\n  env_file: ".env"\ndefaults:\n  models:\n    - "test/model"\n'
    )

    load_config(config_path)

    assert os.environ["OPERATOR_RUNTIME_ENV_TEST"] == "loaded"


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Config not found"):
        load_config(tmp_path / "nonexistent.yaml")
