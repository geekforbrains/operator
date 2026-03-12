from __future__ import annotations

import sqlite3
from typing import Literal

from operator_ai.store import get_store
from operator_ai.tools.context import UserContext, get_user_context, set_user_context
from operator_ai.tools.registry import tool

UserManagementAction = Literal["list", "add", "remove", "link", "unlink", "add_role", "remove_role"]


@tool(
    description="Manage users, identities, and roles. Actions: list, add, remove, link, unlink, add_role, remove_role."
)
async def manage_users(
    action: UserManagementAction,
    username: str = "",
    role: str = "",
    transport: str = "",
    external_id: str = "",
) -> str:
    """Manage users.

    Args:
        action: One of: list, add, remove, link, unlink, add_role, remove_role.
        username: Username (required for add/remove/link/unlink/add_role/remove_role).
        role: Role name (required for add/add_role/remove_role).
        transport: Transport name, e.g. "slack", "telegram" (required for link/unlink).
        external_id: Platform user ID (required for link/unlink).
    """
    store = get_store()

    if action == "list":
        users = store.list_users()
        if not users:
            return "No users."
        lines: list[str] = []
        for u in users:
            parts = [u.username]
            if u.roles:
                parts.append(f"roles={','.join(u.roles)}")
            if u.identities:
                parts.append(f"identities={','.join(u.identities)}")
            lines.append("  ".join(parts))
        return "\n".join(lines)

    if action == "add":
        if not username:
            return "[error: username is required]"
        if not role:
            return "[error: role is required]"
        try:
            store.add_user(username)
        except ValueError as e:
            return f"[error: {e}]"
        except sqlite3.IntegrityError:
            return f"[error: user '{username}' already exists]"
        try:
            store.add_role(username, role)
        except Exception as e:
            return f"[error: {e}]"
        return f"Added user '{username}' with role '{role}'."

    if action == "remove":
        if not username:
            return "[error: username is required]"
        if store.remove_user(username):
            return f"Removed user '{username}'."
        return f"[error: user '{username}' not found]"

    if action == "link":
        if not username:
            return "[error: username is required]"
        if not transport:
            return "[error: transport is required]"
        if not external_id:
            return "[error: external_id is required]"
        platform_id = f"{transport}:{external_id}"
        if store.get_user(username) is None:
            return f"[error: user '{username}' not found]"
        try:
            store.add_identity(username, platform_id)
        except sqlite3.IntegrityError:
            return f"[error: identity '{platform_id}' already linked]"
        return f"Linked {platform_id} to '{username}'."

    if action == "unlink":
        if not username:
            return "[error: username is required]"
        if not transport:
            return "[error: transport is required]"
        if not external_id:
            return "[error: external_id is required]"
        platform_id = f"{transport}:{external_id}"
        if store.remove_identity(platform_id):
            return f"Unlinked {platform_id}."
        return f"[error: identity '{platform_id}' not found]"

    if action == "add_role":
        if not username:
            return "[error: username is required]"
        if not role:
            return "[error: role is required]"
        if store.get_user(username) is None:
            return f"[error: user '{username}' not found]"
        try:
            store.add_role(username, role)
        except sqlite3.IntegrityError:
            return f"[error: user '{username}' already has role '{role}']"
        return f"Added role '{role}' to '{username}'."

    if action == "remove_role":
        if not username:
            return "[error: username is required]"
        if not role:
            return "[error: role is required]"
        if store.remove_role(username, role):
            return f"Removed role '{role}' from '{username}'."
        return f"[error: role '{role}' not found for '{username}']"

    return f"[error: unknown action '{action}'. Use: list, add, remove, link, unlink, add_role, remove_role]"


@tool(description="Set your own timezone using an IANA timezone name such as America/Vancouver.")
async def set_timezone(timezone: str) -> str:
    """Set the current user's timezone.

    Args:
        timezone: IANA timezone name, for example "America/Vancouver".
    """
    user_ctx = get_user_context()
    if user_ctx is None:
        return "[error: timezone can only be set during a user conversation]"

    store = get_store()
    if store.get_user(user_ctx.username) is None:
        return f"[error: user '{user_ctx.username}' not found]"

    try:
        store.set_user_timezone(user_ctx.username, timezone)
    except ValueError as e:
        return f"[error: {e}]"

    set_user_context(
        UserContext(
            username=user_ctx.username,
            roles=list(user_ctx.roles),
            timezone=timezone,
        )
    )
    return f"Timezone set to {timezone}."
