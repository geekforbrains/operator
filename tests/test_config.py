from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from operator_ai.config import (
    Config,
    DefaultsConfig,
    PermissionsConfig,
    RoleConfig,
    SettingsConfig,
    ensure_shared_symlink,
)


def test_timezone_defaults_to_utc() -> None:
    d = DefaultsConfig(models=["test/model"])
    assert d.timezone == "UTC"


def test_timezone_override() -> None:
    d = DefaultsConfig(models=["test/model"], timezone="America/Vancouver")
    assert d.timezone == "America/Vancouver"


def test_config_tz_returns_zoneinfo() -> None:
    c = Config(defaults={"models": ["test/m"], "timezone": "Europe/London"})
    assert c.tz == ZoneInfo("Europe/London")


def test_config_tz_defaults_to_utc() -> None:
    c = Config(defaults={"models": ["test/m"]})
    assert c.tz == ZoneInfo("UTC")


def test_invalid_timezone_raises() -> None:
    with pytest.raises(ValueError, match="Unknown timezone"):
        DefaultsConfig(models=["test/model"], timezone="Mars/Olympus")


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


# ── SettingsConfig ───────────────────────────────────────────


def test_reject_response_defaults_to_ignore() -> None:
    s = SettingsConfig()
    assert s.reject_response == "ignore"


def test_reject_response_announce() -> None:
    s = SettingsConfig(reject_response="announce")
    assert s.reject_response == "announce"


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
