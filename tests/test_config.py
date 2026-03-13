from __future__ import annotations

import logging
import os

import pytest

from operator_ai.config import (
    Config,
    PermissionsConfig,
    RoleConfig,
    RuntimeConfig,
    _load_env_file,
    ensure_shared_symlink,
    load_config,
)

# ── Sandbox ──────────────────────────────────────────────────


def test_agent_sandbox_defaults_to_true() -> None:
    c = Config(defaults={"models": ["test/m"]}, agents={"a": {}})
    assert c.agent_sandbox("a") is True


def test_agent_sandbox_explicit_false() -> None:
    c = Config(defaults={"models": ["test/m"]}, agents={"a": {"sandbox": False}})
    assert c.agent_sandbox("a") is False


def test_agent_sandbox_unknown_agent_defaults_true() -> None:
    c = Config(defaults={"models": ["test/m"]})
    assert c.agent_sandbox("nonexistent") is True


# ── Thinking ─────────────────────────────────────────────────


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


def test_timezone_removed_from_runtime_config() -> None:
    with pytest.raises(ValueError, match="timezone"):
        RuntimeConfig(timezone="UTC")


def test_invalid_thinking_level_raises() -> None:
    with pytest.raises(ValueError, match="thinking"):
        Config(defaults={"models": ["test/m"], "thinking": "max"})


def test_transport_config_normalizes_env_and_settings() -> None:
    config = Config(
        defaults={"models": ["test/m"]},
        agents={
            "operator": {
                "transport": {
                    "type": "slack",
                    "env": {
                        "bot_token": "SLACK_BOT_TOKEN",
                        "app_token": "SLACK_APP_TOKEN",
                    },
                }
            }
        },
    )

    transport = config.agents["operator"].transport
    assert transport is not None
    assert transport.type == "slack"
    assert transport.env["bot_token"] == "SLACK_BOT_TOKEN"
    assert transport.env["app_token"] == "SLACK_APP_TOKEN"
    assert transport.settings["inject_channels_into_prompt"] is True
    assert transport.settings["inject_users_into_prompt"] is True


def test_transport_config_accepts_explicit_settings_mapping() -> None:
    config = Config(
        defaults={"models": ["test/m"]},
        agents={
            "operator": {
                "transport": {
                    "type": "slack",
                    "env": {
                        "bot_token": "SLACK_BOT_TOKEN",
                        "app_token": "SLACK_APP_TOKEN",
                    },
                    "settings": {
                        "inject_users_into_prompt": False,
                    },
                }
            }
        },
    )

    transport = config.agents["operator"].transport
    assert transport is not None
    assert transport.env["bot_token"] == "SLACK_BOT_TOKEN"
    assert transport.env["app_token"] == "SLACK_APP_TOKEN"
    assert transport.settings["inject_users_into_prompt"] is False
    assert transport.settings["inject_channels_into_prompt"] is True


def test_transport_config_requires_slack_env_fields() -> None:
    with pytest.raises(ValueError, match="bot_token"):
        Config(
            defaults={"models": ["test/m"]},
            agents={"operator": {"transport": {"type": "slack", "env": {}}}},
        )


def test_transport_config_rejects_unsupported_type() -> None:
    with pytest.raises(ValueError, match="Unsupported transport type"):
        Config(
            defaults={"models": ["test/m"]},
            agents={"operator": {"transport": {"type": "email"}}},
        )


def test_legacy_defaults_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Config(defaults={"models": ["test/m"], "timezone": "Europe/London"})


def test_legacy_settings_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="settings"):
        Config(defaults={"models": ["test/m"]}, settings={"reject_response": "announce"})


# ── Permissions ──────────────────────────────────────────────


def _cfg(**agent_kwargs) -> Config:
    return Config(defaults={"models": ["test/m"]}, agents={"a": agent_kwargs})


def test_no_permissions_denies_all() -> None:
    c = _cfg()
    assert c.agent_tool_filter("a")("anything") is False
    assert c.agent_skill_filter("a")("anything") is False


def test_permissions_none_denies_all() -> None:
    c = _cfg(permissions={"tools": None, "skills": None})
    assert c.agent_tool_filter("a")("anything") is False
    assert c.agent_skill_filter("a")("anything") is False


def test_permissions_star_allows_all() -> None:
    c = _cfg(permissions={"tools": "*", "skills": "*"})
    assert c.agent_tool_filter("a")("anything") is True
    assert c.agent_skill_filter("a")("anything") is True


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


def test_unknown_agent_denies_all() -> None:
    c = _cfg(permissions={"tools": ["run_shell"]})
    assert c.agent_tool_filter("nonexistent")("run_shell") is False


def test_empty_permissions_denies_all() -> None:
    c = _cfg(permissions={})
    assert c.agent_tool_filter("a")("anything") is False
    assert c.agent_skill_filter("a")("anything") is False


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


# ── Permission groups ────────────────────────────────────────


def _cfg_with_groups(groups: dict, **agent_kwargs) -> Config:
    return Config(
        defaults={"models": ["test/m"]},
        permission_groups=groups,
        agents={"a": agent_kwargs},
    )


def test_group_expansion_in_tool_filter() -> None:
    c = _cfg_with_groups(
        {"memory": ["save_rule", "save_note"], "files": ["read_file", "write_file"]},
        permissions={"tools": ["@memory", "@files"]},
    )
    f = c.agent_tool_filter("a")
    assert f is not None
    assert f("save_rule") is True
    assert f("save_note") is True
    assert f("read_file") is True
    assert f("write_file") is True
    assert f("run_shell") is False


def test_group_expansion_in_skill_filter() -> None:
    c = _cfg_with_groups(
        {"deploy_skills": ["deploy", "rollback"]},
        permissions={"skills": ["@deploy_skills"]},
    )
    f = c.agent_skill_filter("a")
    assert f is not None
    assert f("deploy") is True
    assert f("rollback") is True
    assert f("other") is False


def test_mixed_groups_and_individual_tools() -> None:
    c = _cfg_with_groups(
        {"memory": ["save_rule", "save_note"]},
        permissions={"tools": ["@memory", "run_shell"]},
    )
    f = c.agent_tool_filter("a")
    assert f is not None
    assert f("save_rule") is True
    assert f("save_note") is True
    assert f("run_shell") is True
    assert f("list_files") is False


def test_unknown_group_warns_but_does_not_crash(caplog) -> None:
    c = _cfg_with_groups(
        {},
        permissions={"tools": ["@nonexistent", "run_shell"]},
    )
    with caplog.at_level(logging.WARNING, logger="operator.config"):
        f = c.agent_tool_filter("a")
    assert f is not None
    assert f("run_shell") is True
    assert f("nonexistent") is False
    assert "Unknown permission group '@nonexistent'" in caplog.text


def test_star_still_works_with_groups_defined() -> None:
    c = _cfg_with_groups(
        {"memory": ["save_rule"]},
        permissions={"tools": "*", "skills": "*"},
    )
    assert c.agent_tool_filter("a")("anything") is True
    assert c.agent_skill_filter("a")("anything") is True


def test_empty_group_expands_to_nothing() -> None:
    c = Config(
        defaults={"models": ["test/m"]},
        permission_groups={"empty": []},
        agents={"a": {"permissions": {"tools": ["@empty", "run_shell"]}}},
    )
    f = c.agent_tool_filter("a")
    assert f is not None
    assert f("run_shell") is True
    # The empty group contributes nothing
    assert f("anything_else") is False


def test_no_groups_defined_plain_tools_still_work() -> None:
    c = Config(
        defaults={"models": ["test/m"]},
        agents={"a": {"permissions": {"tools": ["read_file"]}}},
    )
    f = c.agent_tool_filter("a")
    assert f is not None
    assert f("read_file") is True
    assert f("write_file") is False


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
