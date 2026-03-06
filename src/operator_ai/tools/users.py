from __future__ import annotations

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from operator_ai.store import get_store
from operator_ai.tools.registry import tool


@tool(
    description="Manage users, identities, and roles. Actions: list, add, remove, link, unlink, add_role, remove_role."
)
async def manage_users(
    action: str,
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
