"""Typer CLI entrypoint for canary-cage."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .canaries import DocstringCanary, MarkdownCanary, TodoCanary
from .state import CageState, load_state, save_state, state_path

app = typer.Typer(
    name="canary",
    help="🐤 canary-cage — plant prompt-injection tripwires and catch agentjacking in the act.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()

_CANARY_REGISTRY = {
    "markdown": MarkdownCanary,
    "docstring": DocstringCanary,
    "todo": TodoCanary,
}

_ALL_TYPES = tuple(_CANARY_REGISTRY)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"canary-cage {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Root callback — exposes --version."""


@app.command()
def hello(name: str = typer.Argument("world", help="Who to greet.")) -> None:
    """Sanity-check command. Chirp at the user."""
    console.print(f"🐤 hello, {name} — the cage is empty (for now).")


_TYPE_OPTION = typer.Option(
    "all",
    "--type",
    "-t",
    help="Canary type to plant: markdown, docstring, todo, or 'all'.",
)
_ROOT_OPTION = typer.Option(
    None,
    "--root",
    help="Repo root to operate on (defaults to cwd).",
    file_okay=False,
    dir_okay=True,
    resolve_path=True,
)


def _resolve_root(root: Path | None) -> Path:
    return root if root is not None else Path.cwd()


@app.command()
def plant(
    type: str = _TYPE_OPTION,
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Plant canaries across the repo and record them in state."""

    root = _resolve_root(root)
    if type == "all":
        types = _ALL_TYPES
    elif type in _CANARY_REGISTRY:
        types = (type,)
    else:
        known = ", ".join((*sorted(_CANARY_REGISTRY), "all"))
        console.print(f"[red]unknown canary type: {type!r} (known: {known})[/red]")
        raise typer.Exit(code=2)

    state = load_state(root)
    newly_planted = []
    for t in types:
        newly_planted.extend(_CANARY_REGISTRY[t]().plant(root))

    if not newly_planted:
        console.print("[yellow]no eligible files found to plant in.[/yellow]")
        # Still write state to materialize the cage dir on first run.
        save_state(root, state)
        return

    state.canaries.extend(newly_planted)
    save_state(root, state)
    console.print(
        f"🐤 planted [bold]{len(newly_planted)}[/bold] canar"
        f"{'y' if len(newly_planted) == 1 else 'ies'} → {state_path(root)}"
    )


@app.command("list")
def list_(
    root: Path | None = _ROOT_OPTION,
) -> None:
    """List every planted canary in a Rich table."""

    root = _resolve_root(root)
    state = load_state(root)
    if not state.canaries:
        console.print("[yellow]no canaries planted — run `canary plant`.[/yellow]")
        return

    table = Table(title=f"🐤 canary-cage ({len(state.canaries)} planted)")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("type", style="magenta")
    table.add_column("path", style="green")
    table.add_column("planted_at", style="dim")
    for c in sorted(state.canaries, key=lambda x: (x.type, x.path)):
        table.add_row(
            c.id,
            c.type,
            c.path,
            c.planted_at.strftime("%Y-%m-%d %H:%M:%SZ"),
        )
    console.print(table)


@app.command()
def check() -> None:
    """Scan for evidence a canary fired. (not implemented yet — M4)"""
    console.print("[yellow]check: not implemented yet — see PLAN.md M4.[/yellow]")
    raise typer.Exit(code=2)


@app.command()
def uproot(
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Remove every planted canary and clear state."""

    root = _resolve_root(root)
    state = load_state(root)
    if not state.canaries:
        console.print("[yellow]no canaries planted — nothing to uproot.[/yellow]")
        return

    removed = 0
    for planted in state.canaries:
        canary_cls = _CANARY_REGISTRY.get(planted.type)
        if canary_cls is None:
            console.print(
                f"[red]skipping unknown canary type {planted.type!r} ({planted.id})[/red]"
            )
            continue
        canary_cls().uproot(root, planted)
        removed += 1

    save_state(root, CageState())
    console.print(
        f"🧹 uprooted [bold]{removed}[/bold] canar"
        f"{'y' if removed == 1 else 'ies'}."
    )


if __name__ == "__main__":
    app()
