from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from operator_ai.cli.common import cli_base_dir, cli_memory_store, load_cli_config
from operator_ai.memory import MemoryIndex, MemoryStore, reindex_diff, reindex_full

console = Console()

memory_app = typer.Typer(help="Browse and search memories.")


@memory_app.callback(invoke_without_command=True)
def memory_main(ctx: typer.Context) -> None:
    """Browse and search file-backed memories."""
    if ctx.invoked_subcommand is None:
        memory_list_cmd(scope="global")


@memory_app.command("list")
def memory_list_cmd(
    scope: str = typer.Argument("global", help="Scope: global, agent:<name>, or user:<name>."),
) -> None:
    """List rules and notes for a scope."""
    mem = cli_memory_store(load_cli_config())
    rules = mem.list_rules(scope)
    notes = mem.list_notes(scope)

    if not rules and not notes:
        console.print(f"No memories in scope '{scope}'.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Type", style="bold")
    table.add_column("Path", style="dim")
    table.add_column("Updated", style="dim")
    table.add_column("Expires", style="dim")
    table.add_column("Content")

    for mf in rules:
        content = mf.content.replace("\n", " ")
        if len(content) > 80:
            content = content[:77] + "..."
        updated = mf.updated_at.strftime("%Y-%m-%d %H:%M") if mf.updated_at else "-"
        expires = mf.expires_at.strftime("%Y-%m-%d %H:%M") if mf.expires_at else "-"
        table.add_row("rule", mf.relative_path, updated, expires, content)

    for mf in notes:
        content = mf.content.replace("\n", " ")
        if len(content) > 80:
            content = content[:77] + "..."
        updated = mf.updated_at.strftime("%Y-%m-%d %H:%M") if mf.updated_at else "-"
        expires = mf.expires_at.strftime("%Y-%m-%d %H:%M") if mf.expires_at else "-"
        table.add_row("note", mf.relative_path, updated, expires, content)

    console.print(table)


@memory_app.command("search")
def memory_search_cmd(
    query: str = typer.Argument(help="Search query."),
    scope: str = typer.Option("global", "--scope", "-s", help="Scope to search."),
) -> None:
    """Search notes by filename and content."""
    mem = cli_memory_store(load_cli_config())
    results = mem.search_notes(scope, query)

    if not results:
        console.print(f"No notes matching '{query}' in scope '{scope}'.")
        raise typer.Exit()

    table = Table(show_header=True, show_edge=False, pad_edge=False)
    table.add_column("Path", style="dim")
    table.add_column("Updated", style="dim")
    table.add_column("Content")

    for mf in results:
        content = mf.content.replace("\n", " ")
        if len(content) > 80:
            content = content[:77] + "..."
        updated = mf.updated_at.strftime("%Y-%m-%d %H:%M") if mf.updated_at else "-"
        table.add_row(mf.relative_path, updated, content)

    console.print(table)


@memory_app.command("index")
def memory_index_cmd(
    force: bool = typer.Option(False, "--force", help="Full rebuild instead of hash-diff."),
) -> None:
    """Rebuild the FTS5 search index from memory files on disk."""
    config = load_cli_config()
    base_dir = cli_base_dir(config)
    index_db = base_dir / "db" / "memory_index.db"
    index = MemoryIndex(index_db)
    mem = MemoryStore(base_dir=base_dir, index=index)

    if force:
        count = reindex_full(mem, index)
        console.print(f"Full reindex complete: {count} files indexed.")
    else:
        upserted, deleted = reindex_diff(mem, index)
        console.print(f"Reindex complete: {upserted} updated, {deleted} removed.")

    index.close()
