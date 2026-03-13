from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from operator_ai.config import ConfigError, load_config
from operator_ai.message_timestamps import format_ts
from operator_ai.store import get_store
from operator_ai.user_ops import (
    UserOperationError,
    add_role,
    create_user,
    link_identity,
    platform_id,
    remove_role,
    remove_user,
    unlink_identity,
)

console = Console()

user_app = typer.Typer(help="Manage users, identities, and roles.")


@user_app.command("add")
def user_add(
    username: str = typer.Argument(help="Username (lowercase alphanumeric, dots, hyphens)."),
    transport: str = typer.Argument(help="Transport name (e.g. slack, telegram)."),
    external_id: str = typer.Argument(help="External ID on that transport."),
    role: str = typer.Option(..., "--role", "-r", help="Role to assign."),
) -> None:
    """Create a user with an initial identity and role."""
    if role != "admin":
        try:
            config = load_config()
            if role not in config.roles:
                console.print(f"[yellow]Warning:[/yellow] role '{role}' is not defined in config.")
        except ConfigError:
            pass

    store = get_store()
    try:
        identity = platform_id(transport, external_id)
        create_user(store, username=username, role=role, identity=identity)
    except UserOperationError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None

    console.print(f"User '{username}' created with role '{role}' and identity '{identity}'.")


@user_app.command("link")
def user_link(
    username: str = typer.Argument(help="Username."),
    transport: str = typer.Argument(help="Transport name."),
    external_id: str = typer.Argument(help="External ID on that transport."),
) -> None:
    """Link a transport identity to an existing user."""
    store = get_store()
    try:
        identity = platform_id(transport, external_id)
        link_identity(store, username=username, identity=identity)
    except UserOperationError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    console.print(f"Linked '{identity}' to user '{username}'.")


@user_app.command("unlink")
def user_unlink(
    username: str = typer.Argument(help="Username."),
    transport: str = typer.Argument(help="Transport name."),
    external_id: str = typer.Argument(help="External ID on that transport."),
) -> None:
    """Remove a transport identity from a user."""
    store = get_store()
    try:
        identity = platform_id(transport, external_id)
        unlink_identity(store, username=username, identity=identity)
    except UserOperationError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    console.print(f"Unlinked '{identity}' from user '{username}'.")


@user_app.command("remove")
def user_remove(
    username: str = typer.Argument(help="Username to remove."),
) -> None:
    """Remove a user entirely (cascades identities and roles)."""
    store = get_store()
    try:
        remove_user(store, username=username)
    except UserOperationError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    console.print(f"User '{username}' removed.")


@user_app.command("list")
def user_list() -> None:
    """List all users with identities and roles."""
    store = get_store()
    users = store.list_users()
    if not users:
        console.print("No users found.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Username", style="bold")
    table.add_column("Roles")
    table.add_column("Identities")
    for user in users:
        roles = ", ".join(user.roles) if user.roles else "-"
        identities = ", ".join(user.identities) if user.identities else "-"
        table.add_row(user.username, roles, identities)
    console.print(table)


@user_app.command("info")
def user_info(
    username: str = typer.Argument(help="Username to inspect."),
) -> None:
    """Show details for one user."""
    store = get_store()
    user = store.get_user(username)
    if user is None:
        console.print(f"[red]Error:[/red] user '{username}' not found.")
        raise typer.Exit(code=1)

    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column("Key", style="bold", min_width=12)
    table.add_column("Value")
    table.add_row("Username", user.username)
    table.add_row("Created", format_ts(user.created_at))
    table.add_row("Roles", ", ".join(user.roles) if user.roles else "-")
    table.add_row(
        "Identities",
        ", ".join(user.identities) if user.identities else "-",
    )
    console.print(table)


@user_app.command("add-role")
def user_add_role(
    username: str = typer.Argument(help="Username."),
    role: str = typer.Argument(help="Role to add."),
) -> None:
    """Add a role to a user."""
    store = get_store()
    try:
        add_role(store, username=username, role=role)
    except UserOperationError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    console.print(f"Added role '{role}' to user '{username}'.")


@user_app.command("remove-role")
def user_remove_role(
    username: str = typer.Argument(help="Username."),
    role: str = typer.Argument(help="Role to remove."),
) -> None:
    """Remove a role from a user."""
    store = get_store()
    try:
        remove_role(store, username=username, role=role)
    except UserOperationError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from None
    console.print(f"Removed role '{role}' from user '{username}'.")
