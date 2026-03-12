from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from operator_ai.transport.base import Transport

if TYPE_CHECKING:
    from operator_ai.store import Store


@dataclass(frozen=True)
class TransportDefinition:
    type_name: str
    create_transport: Callable[[str, str, dict[str, Any], dict[str, Any], Store], Transport]
    normalize_config: Callable[
        [dict[str, Any], dict[str, Any]],
        tuple[dict[str, Any], dict[str, Any]],
    ]
    secret_env_vars: Callable[[dict[str, Any], dict[str, Any]], set[str]]
    logger_names: tuple[str, ...] = ()


_DEFINITIONS: dict[str, TransportDefinition] | None = None


def _load_definitions() -> dict[str, TransportDefinition]:
    global _DEFINITIONS
    if _DEFINITIONS is None:
        from operator_ai.transport.slack import SLACK_TRANSPORT_DEFINITION

        _DEFINITIONS = {
            SLACK_TRANSPORT_DEFINITION.type_name: SLACK_TRANSPORT_DEFINITION,
        }
    return _DEFINITIONS


def get_transport_definition(type_name: str) -> TransportDefinition | None:
    return _load_definitions().get(type_name.strip().lower())


def list_transport_definitions() -> list[TransportDefinition]:
    return list(_load_definitions().values())


def normalize_transport_config(
    type_name: str,
    env: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    definition = get_transport_definition(type_name)
    if definition is None:
        raise ValueError(f"Unsupported transport type: {type_name!r}")
    return definition.normalize_config(env, settings)


def create_transport(
    *,
    type_name: str,
    name: str,
    agent_name: str,
    env: dict[str, Any],
    settings: dict[str, Any],
    store: Store,
) -> Transport:
    definition = get_transport_definition(type_name)
    if definition is None:
        raise ValueError(f"Unsupported transport type: {type_name!r}")
    return definition.create_transport(name, agent_name, env, settings, store)


def transport_secret_env_vars(
    type_name: str,
    env: dict[str, Any],
    settings: dict[str, Any],
) -> set[str]:
    definition = get_transport_definition(type_name)
    if definition is None:
        return set()
    return definition.secret_env_vars(env, settings)


def transport_logger_names() -> tuple[str, ...]:
    names: list[str] = []
    for definition in list_transport_definitions():
        names.extend(definition.logger_names)
    return tuple(dict.fromkeys(names))
