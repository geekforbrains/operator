from __future__ import annotations

import sqlite3

from operator_ai.store import Store


class UserOperationError(Exception):
    """Raised when a user-management operation cannot be completed."""


def platform_id(transport: str, external_id: str) -> str:
    return f"{transport}:{external_id}"


def create_user(
    store: Store,
    *,
    username: str,
    role: str,
    identity: str | None = None,
) -> None:
    created_user = False
    try:
        store.add_user(username)
        created_user = True
        store.add_role(username, role)
        if identity:
            store.add_identity(username, identity)
    except ValueError as e:
        raise UserOperationError(str(e)) from None
    except sqlite3.IntegrityError as e:
        if created_user:
            store.remove_user(username)
        if identity and store.resolve_username(identity) is not None:
            raise UserOperationError(f"identity '{identity}' already linked") from None
        raise UserOperationError(f"user '{username}' already exists") from e
    except Exception as e:
        if created_user:
            store.remove_user(username)
        raise UserOperationError(str(e)) from e


def remove_user(store: Store, *, username: str) -> None:
    if not store.remove_user(username):
        raise UserOperationError(f"user '{username}' not found")


def link_identity(store: Store, *, username: str, identity: str) -> None:
    if store.get_user(username) is None:
        raise UserOperationError(f"user '{username}' not found")
    try:
        store.add_identity(username, identity)
    except sqlite3.IntegrityError:
        raise UserOperationError(f"identity '{identity}' already linked") from None


def unlink_identity(store: Store, *, username: str, identity: str) -> None:
    if store.get_user(username) is None:
        raise UserOperationError(f"user '{username}' not found")
    owner = store.resolve_username(identity)
    if owner is None:
        raise UserOperationError(f"identity '{identity}' not found")
    if owner != username:
        raise UserOperationError(f"identity '{identity}' belongs to '{owner}', not '{username}'")
    if not store.remove_identity(identity):
        raise UserOperationError(f"identity '{identity}' not found")


def add_role(store: Store, *, username: str, role: str) -> None:
    if store.get_user(username) is None:
        raise UserOperationError(f"user '{username}' not found")
    try:
        store.add_role(username, role)
    except sqlite3.IntegrityError:
        raise UserOperationError(f"user '{username}' already has role '{role}'") from None


def remove_role(store: Store, *, username: str, role: str) -> None:
    if store.remove_role(username, role):
        return
    if store.get_user(username) is None:
        raise UserOperationError(f"user '{username}' not found")
    raise UserOperationError(f"role '{role}' not found for '{username}'")


def set_timezone(store: Store, *, username: str, timezone: str) -> None:
    if store.get_user(username) is None:
        raise UserOperationError(f"user '{username}' not found")
    try:
        store.set_user_timezone(username, timezone)
    except ValueError as e:
        raise UserOperationError(str(e)) from None
