from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo

import litellm
import pytest

from operator_ai.config import (
    Config,
    MemoryConfig,
    PermissionsConfig,
    RoleConfig,
    RuntimeConfig,
    TransportConfig,
    _load_env_file,
    ensure_shared_symlink,
    load_config,
)
from operator_ai.litellm_logging import configure_litellm_logging


@pytest.fixture
def restore_litellm_loggers():
    logger_state = []
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logger = logging.getLogger(name)
        logger_state.append(
            (
                logger,
                list(logger.handlers),
                logger.level,
                logger.propagate,
                logger.disabled,
            )
        )
    suppress_debug_info = litellm.suppress_debug_info
    try:
        yield
    finally:
        litellm.suppress_debug_info = suppress_debug_info
        for logger, handlers, level, propagate, disabled in logger_state:
            logger.handlers.clear()
            for handler in handlers:
                logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


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
    c = Config(defaults={"models": ["test/m"]})
    assert c.tz == ZoneInfo("UTC")


def test_agent_thinking_defaults_to_off() -> None:
    c = Config(defaults={"models": ["test/m"]})
    assert c.agent_thinking("operator") == "off"


def test_agent_thinking_agent_override_wins() -> None:
    c = Config(
        defaults={"models": ["test/m"], "thinking": "low"},
        agents={"operator": {"thinking": "high"}},
    )
    assert c.agent_thinking("operator") == "high"
    assert c.agent_thinking("other") == "low"


def test_invalid_timezone_raises() -> None:
    with pytest.raises(ValueError, match="Unknown timezone"):
        RuntimeConfig(timezone="Mars/Olympus")


def test_invalid_thinking_level_raises() -> None:
    with pytest.raises(ValueError, match="thinking"):
        Config(defaults={"models": ["test/m"], "thinking": "max"})


def test_legacy_defaults_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Config(defaults={"models": ["test/m"], "timezone": "Europe/London"})


def test_legacy_settings_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="settings"):
        Config(defaults={"models": ["test/m"]}, settings={"reject_response": "announce"})


# ── Permissions ──────────────────────────────────────────────


def _cfg(**agent_kwargs) -> Config:
    return Config(defaults={"models": ["test/m"]}, agents={"a": agent_kwargs})


def test_no_permissions_returns_none_filters() -> None:
    c = _cfg()
    assert c.agent_tool_filter("a") is None
    assert c.agent_skill_filter("a") is None


def test_permissions_none_means_unrestricted() -> None:
    c = _cfg(permissions={"tools": None, "skills": None})
    assert c.agent_tool_filter("a") is None
    assert c.agent_skill_filter("a") is None


def test_permissions_star_means_unrestricted() -> None:
    c = _cfg(permissions={"tools": "*", "skills": "*"})
    assert c.agent_tool_filter("a") is None
    assert c.agent_skill_filter("a") is None


def test_tool_list_filter() -> None:
    c = _cfg(permissions={"tools": ["read_file", "list_files"]})
    f = c.agent_tool_filter("a")
    assert f is not None
    assert f("read_file") is True
    assert f("run_shell") is False


def test_skill_list_filter() -> None:
    c = _cfg(permissions={"skills": ["deploy"]})
    f = c.agent_skill_filter("a")
    assert f is not None
    assert f("deploy") is True
    assert f("other") is False


def test_unknown_agent_returns_none_filter() -> None:
    c = _cfg(permissions={"tools": ["run_shell"]})
    assert c.agent_tool_filter("nonexistent") is None


def test_empty_permissions_returns_none_filter() -> None:
    c = _cfg(permissions={})
    assert c.agent_tool_filter("a") is None
    assert c.agent_skill_filter("a") is None


# ── Flat permissions model ───────────────────────────────────


def test_permissions_config_defaults() -> None:
    p = PermissionsConfig()
    assert p.tools is None
    assert p.skills is None


def test_permissions_config_star() -> None:
    p = PermissionsConfig(tools="*", skills="*")
    assert p.tools == "*"
    assert p.skills == "*"


def test_permissions_config_list() -> None:
    p = PermissionsConfig(tools=["a", "b"], skills=["c"])
    assert p.tools == ["a", "b"]
    assert p.skills == ["c"]


# ── RoleConfig ───────────────────────────────────────────────


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


# ── RuntimeConfig ────────────────────────────────────────────


def test_runtime_defaults() -> None:
    runtime = RuntimeConfig()
    assert runtime.show_usage is False
    assert runtime.reject_response == "ignore"


def test_runtime_reject_response_announce() -> None:
    runtime = RuntimeConfig(reject_response="announce")
    assert runtime.reject_response == "announce"


def test_transport_config_slack_channel_defaults() -> None:
    transport = TransportConfig(
        type="slack",
        bot_token_env="SLACK_BOT_TOKEN",
        app_token_env="SLACK_APP_TOKEN",
    )
    assert transport.options["bot_token_env"] == "SLACK_BOT_TOKEN"
    assert transport.options["app_token_env"] == "SLACK_APP_TOKEN"
    assert transport.options["include_archived_channels"] is False
    assert transport.options["inject_channels_into_prompt"] is True
    assert transport.options["inject_users_into_prompt"] is True


def test_transport_config_slack_channel_overrides() -> None:
    transport = TransportConfig(
        type="slack",
        bot_token_env="SLACK_BOT_TOKEN",
        app_token_env="SLACK_APP_TOKEN",
        include_archived_channels=True,
        inject_channels_into_prompt=True,
    )
    assert transport.options["include_archived_channels"] is True
    assert transport.options["inject_channels_into_prompt"] is True


# ── MemoryConfig ─────────────────────────────────────────────


def test_memory_config_candidate_ttl_default() -> None:
    memory = MemoryConfig()
    assert memory.candidate_ttl_days == 14


def test_memory_config_candidate_ttl_override() -> None:
    config = Config(
        defaults={"models": ["test/m"]},
        memory={"candidate_ttl_days": 21},
    )
    assert config.memory.candidate_ttl_days == 21


# ── Shared symlink ───────────────────────────────────────────


def test_ensure_shared_symlink(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"

    ensure_shared_symlink(workspace, shared)

    link = workspace / "shared"
    assert link.is_symlink()
    assert link.resolve() == shared.resolve()
    assert shared.is_dir()


def test_ensure_shared_symlink_idempotent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"

    ensure_shared_symlink(workspace, shared)
    ensure_shared_symlink(workspace, shared)  # should not raise

    assert (workspace / "shared").is_symlink()


def test_ensure_shared_symlink_skips_non_symlink(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"
    # Create a real directory at the link target
    (workspace / "shared").mkdir()

    ensure_shared_symlink(workspace, shared)

    # Should not have replaced the real directory
    assert not (workspace / "shared").is_symlink()
    assert (workspace / "shared").is_dir()


# ── _load_env_file ──────────────────────────────────────────


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_VAR=from_file\n")
    monkeypatch.setenv("MY_TEST_VAR", "original")

    _load_env_file(str(env_file))

    assert os.environ["MY_TEST_VAR"] == "original"


def test_load_env_file_strips_quotes(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("QUOTED_VAR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('QUOTED_VAR="hello world"\n')

    _load_env_file(str(env_file))

    assert os.environ["QUOTED_VAR"] == "hello world"


def test_load_env_file_skips_comments_and_blanks(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VALID_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nVALID_KEY=yes\n")

    _load_env_file(str(env_file))

    assert os.environ["VALID_KEY"] == "yes"


def test_load_env_file_missing_file_is_noop(tmp_path) -> None:
    _load_env_file(str(tmp_path / "nonexistent.env"))


def test_load_config_reads_runtime_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPERATOR_RUNTIME_ENV_TEST", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPERATOR_RUNTIME_ENV_TEST=loaded\n")
    config_path = tmp_path / "operator.yaml"
    config_path.write_text(
        'runtime:\n  env_file: ".env"\ndefaults:\n  models:\n    - "test/model"\n'
    )

    load_config(config_path)

    assert os.environ["OPERATOR_RUNTIME_ENV_TEST"] == "loaded"


def test_load_config_applies_litellm_log_level_from_runtime_env_file(
    tmp_path,
    monkeypatch,
    restore_litellm_loggers,  # noqa: ARG001
) -> None:
    monkeypatch.delenv("LITELLM_LOG", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LITELLM_LOG=DEBUG\n")
    config_path = tmp_path / "operator.yaml"
    config_path.write_text(
        'runtime:\n  env_file: ".env"\ndefaults:\n  models:\n    - "test/model"\n'
    )

    load_config(config_path)

    assert os.environ["LITELLM_LOG"] == "DEBUG"
    assert litellm.suppress_debug_info is True
    assert logging.getLogger("LiteLLM").level == logging.DEBUG


def test_configure_litellm_logging_reuses_operator_handlers(
    monkeypatch,
    restore_litellm_loggers,  # noqa: ARG001
) -> None:
    monkeypatch.delenv("LITELLM_LOG", raising=False)
    operator_logger = logging.getLogger("operator.test")
    saved_handlers = list(operator_logger.handlers)
    saved_level = operator_logger.level
    saved_propagate = operator_logger.propagate
    handler = logging.StreamHandler()
    try:
        operator_logger.handlers.clear()
        operator_logger.addHandler(handler)
        operator_logger.setLevel(logging.DEBUG)
        operator_logger.propagate = False

        configure_litellm_logging(operator_logger_name="operator.test")

        llm_logger = logging.getLogger("LiteLLM")
        assert litellm.suppress_debug_info is True
        assert llm_logger.handlers == [handler]
        assert llm_logger.propagate is False
        assert llm_logger.level == logging.WARNING
    finally:
        operator_logger.handlers.clear()
        for existing in saved_handlers:
            operator_logger.addHandler(existing)
        operator_logger.setLevel(saved_level)
        operator_logger.propagate = saved_propagate
