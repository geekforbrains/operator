"""Tests for the tool registry: registration, schema generation, and permission filtering."""

from __future__ import annotations

from typing import Literal

from operator_ai.config import Config
from operator_ai.tools.registry import ToolDef, _build_parameters, get_tools

# ── Tool registration ────────────────────────────────────────────


def test_tool_decorator_registers() -> None:
    """The @tool decorator registers the function in the global registry."""
    tools = get_tools()
    names = {t.name for t in tools}
    # Spot-check a few tools that should exist from the tool modules
    assert "read_file" in names
    assert "write_file" in names
    assert "run_shell" in names
    assert "web_fetch" in names


def test_all_registered_tools_have_schemas() -> None:
    """Every registered tool should have a valid OpenAI tool schema."""
    for td in get_tools():
        schema = td.to_openai_tool()
        assert schema["type"] == "function"
        func = schema["function"]
        assert isinstance(func["name"], str) and func["name"]
        assert isinstance(func["description"], str) and func["description"]
        assert isinstance(func["parameters"], dict)
        assert func["parameters"]["type"] == "object"


def test_tool_names_are_unique() -> None:
    """No two tools should share the same name."""
    tools = get_tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), (
        f"Duplicate tool names: {[n for n in names if names.count(n) > 1]}"
    )


# ── Schema generation ────────────────────────────────────────────


def test_schema_from_simple_function() -> None:
    """Schema generation from a function with typed args."""

    def example(name: str, count: int) -> str:
        """Example function.

        Args:
            name: The name.
            count: How many.
        """
        return f"{name}: {count}"

    params = _build_parameters(example)
    assert params["type"] == "object"
    assert "name" in params["properties"]
    assert "count" in params["properties"]
    assert params["properties"]["name"]["type"] == "string"
    assert params["properties"]["count"]["type"] == "integer"
    assert "name" in params["required"]
    assert "count" in params["required"]


def test_schema_optional_params() -> None:
    """Parameters with defaults should not be required."""

    def example(query: str, limit: int = 10) -> str:  # noqa: ARG001
        """Example.

        Args:
            query: Search query.
            limit: Max results.
        """
        return query

    params = _build_parameters(example)
    assert "query" in params["required"]
    assert "limit" not in params.get("required", [])


def test_schema_docstring_descriptions() -> None:
    """Descriptions from Google-style docstrings are extracted."""

    def example(path: str, content: str) -> str:  # noqa: ARG001
        """Write something.

        Args:
            path: The file path.
            content: The file content.
        """
        return ""

    params = _build_parameters(example)
    assert params["properties"]["path"]["description"] == "The file path."
    assert params["properties"]["content"]["description"] == "The file content."


def test_schema_bool_param() -> None:
    """Boolean parameters map to JSON boolean type."""

    def example(verbose: bool = False) -> str:  # noqa: ARG001
        """Example.

        Args:
            verbose: Enable verbose output.
        """
        return ""

    params = _build_parameters(example)
    assert params["properties"]["verbose"]["type"] == "boolean"


def test_schema_float_param() -> None:
    """Float parameters map to JSON number type."""

    def example(threshold: float = 0.5) -> str:  # noqa: ARG001
        """Example.

        Args:
            threshold: Score threshold.
        """
        return ""

    params = _build_parameters(example)
    assert params["properties"]["threshold"]["type"] == "number"


def test_schema_literal_enum() -> None:
    """Literal string parameters become enums."""

    def example(scope: Literal["agent", "user", "global"] = "agent") -> str:  # noqa: ARG001
        """Example.

        Args:
            scope: Memory scope.
        """
        return ""

    params = _build_parameters(example)
    assert params["properties"]["scope"]["type"] == "string"
    assert params["properties"]["scope"]["enum"] == ["agent", "user", "global"]


def test_schema_scalar_union_uses_any_of() -> None:
    """Scalar unions preserve their member types."""

    def example(value: str | int | float | bool) -> str:  # noqa: ARG001
        """Example.

        Args:
            value: Scalar value.
        """
        return ""

    params = _build_parameters(example)
    any_of = params["properties"]["value"]["anyOf"]
    assert {schema["type"] for schema in any_of} == {"string", "integer", "number", "boolean"}


def test_schema_optional_param_preserves_inner_type() -> None:
    """Optional parameters should use the inner type and remain optional by default."""

    def example(label: str | None = None) -> str:  # noqa: ARG001
        """Example.

        Args:
            label: Optional label.
        """
        return ""

    params = _build_parameters(example)
    assert params["properties"]["label"]["type"] == "string"
    assert "label" not in params.get("required", [])


def test_schema_array_param_preserves_item_type() -> None:
    """Simple list annotations become array schemas."""

    def example(names: list[str]) -> str:  # noqa: ARG001
        """Example.

        Args:
            names: Input names.
        """
        return ""

    params = _build_parameters(example)
    assert params["properties"]["names"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "Input names.",
    }


def test_schema_no_self_cls() -> None:
    """self and cls parameters should be excluded from schema."""

    class Foo:
        def method(self, name: str) -> str:
            return name

        @classmethod
        def class_method(cls, name: str) -> str:
            return name

    params_method = _build_parameters(Foo.method)
    assert "self" not in params_method["properties"]
    assert "name" in params_method["properties"]

    params_cls = _build_parameters(Foo.class_method)
    assert "cls" not in params_cls["properties"]


def test_tooldef_to_openai_tool() -> None:
    """ToolDef.to_openai_tool produces correct structure."""

    def my_tool(query: str) -> str:  # noqa: ARG001
        """Search.

        Args:
            query: The search query.
        """
        return ""

    td = ToolDef(my_tool, "Search for things")
    schema = td.to_openai_tool()
    assert schema == {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "Search for things",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def test_registered_tool_schemas_preserve_real_enums_and_unions() -> None:
    """Real tools should expose their closed sets and scalar unions."""

    tools = {tool.name: tool for tool in get_tools()}

    manage_users_action = tools["manage_users"].parameters["properties"]["action"]
    assert manage_users_action["type"] == "string"
    assert manage_users_action["enum"] == [
        "list",
        "add",
        "remove",
        "link",
        "unlink",
        "add_role",
        "remove_role",
    ]

    state_value = tools["set_state"].parameters["properties"]["value"]
    assert {schema["type"] for schema in state_value["anyOf"]} == {
        "string",
        "integer",
        "number",
        "boolean",
    }

    memory_scope = tools["save_rule"].parameters["properties"]["scope"]
    assert memory_scope["type"] == "string"
    assert memory_scope["enum"] == ["agent", "user", "global"]


# ── Permission filtering (injection layer) ───────────────────────


def test_tool_filter_allow_all() -> None:
    """Wildcard '*' permissions allow all tools."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={
            "operator": {
                "permissions": {"tools": "*", "skills": "*"},
            },
        },
    )
    tool_filter = config.agent_tool_filter("operator")
    assert tool_filter("read_file") is True
    assert tool_filter("run_shell") is True
    assert tool_filter("anything") is True


def test_tool_filter_allow_list() -> None:
    """Only listed tools are allowed."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={
            "operator": {
                "permissions": {"tools": ["read_file", "list_files"]},
            },
        },
    )
    tool_filter = config.agent_tool_filter("operator")
    assert tool_filter("read_file") is True
    assert tool_filter("list_files") is True
    assert tool_filter("run_shell") is False
    assert tool_filter("web_fetch") is False


def test_tool_filter_empty_list() -> None:
    """Empty tools list blocks everything."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={
            "operator": {
                "permissions": {"tools": [], "skills": []},
            },
        },
    )
    tool_filter = config.agent_tool_filter("operator")
    assert tool_filter("read_file") is False
    assert tool_filter("anything") is False


def test_tool_filter_no_permissions() -> None:
    """No permissions block means nothing is allowed (closed-by-default)."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={"operator": {}},
    )
    tool_filter = config.agent_tool_filter("operator")
    assert tool_filter("read_file") is False
    assert tool_filter("anything") is False


def test_tool_filter_no_agent_config() -> None:
    """Unknown agent returns deny-all filter."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={},
    )
    tool_filter = config.agent_tool_filter("nonexistent")
    assert tool_filter("read_file") is False


def test_skill_filter_allow_all() -> None:
    """Wildcard '*' skill permissions allow all skills."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={
            "operator": {
                "permissions": {"tools": "*", "skills": "*"},
            },
        },
    )
    skill_filter = config.agent_skill_filter("operator")
    assert skill_filter("research") is True
    assert skill_filter("anything") is True


def test_skill_filter_allow_list() -> None:
    """Only listed skills are allowed."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={
            "operator": {
                "permissions": {"tools": "*", "skills": ["research"]},
            },
        },
    )
    skill_filter = config.agent_skill_filter("operator")
    assert skill_filter("research") is True
    assert skill_filter("deploy") is False


def test_skill_filter_closed_by_default() -> None:
    """No permissions block means no skills allowed."""
    config = Config(
        defaults={"models": ["test/model"]},
        agents={"operator": {}},
    )
    skill_filter = config.agent_skill_filter("operator")
    assert skill_filter("anything") is False


# ── Tool filtering in agent loop ─────────────────────────────────


def test_tool_filter_reduces_tool_list() -> None:
    """Applying a filter to the tool list removes non-allowed tools."""
    all_tools = get_tools()
    assert len(all_tools) > 0

    # Allow only read_file and list_files
    allowed = {"read_file", "list_files"}
    filtered = [t for t in all_tools if t.name in allowed]
    assert len(filtered) <= len(allowed)
    for t in filtered:
        assert t.name in allowed

    # Verify filtered-out tools are not present
    filtered_names = {t.name for t in filtered}
    for t in all_tools:
        if t.name not in allowed:
            assert t.name not in filtered_names
