from __future__ import annotations

from operator_ai.config import RoleConfig
from operator_ai.main import resolve_allowed_agents


def test_admin_role_returns_none() -> None:
    roles = ["admin"]
    config_roles: dict[str, RoleConfig] = {
        "viewer": RoleConfig(agents=["agent-a"]),
    }
    assert resolve_allowed_agents(roles, config_roles) is None


def test_specific_roles_return_correct_agents() -> None:
    roles = ["viewer"]
    config_roles = {
        "viewer": RoleConfig(agents=["agent-a", "agent-b"]),
        "editor": RoleConfig(agents=["agent-c"]),
    }
    result = resolve_allowed_agents(roles, config_roles)
    assert result == {"agent-a", "agent-b"}


def test_unknown_role_gives_empty_set() -> None:
    roles = ["unknown"]
    config_roles = {
        "viewer": RoleConfig(agents=["agent-a"]),
    }
    result = resolve_allowed_agents(roles, config_roles)
    assert result == set()


def test_multiple_roles_union_agents() -> None:
    roles = ["viewer", "editor"]
    config_roles = {
        "viewer": RoleConfig(agents=["agent-a", "agent-b"]),
        "editor": RoleConfig(agents=["agent-b", "agent-c"]),
    }
    result = resolve_allowed_agents(roles, config_roles)
    assert result == {"agent-a", "agent-b", "agent-c"}
