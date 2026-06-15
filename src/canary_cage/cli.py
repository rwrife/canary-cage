"""Typer CLI entrypoint for canary-cage."""

from __future__ import annotations

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(
    name="canary",
    help="🐤 canary-cage — plant prompt-injection tripwires and catch agentjacking in the act.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


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


# Placeholder commands so `--help` reflects the planned surface.
# Real implementations land in M2+.

@app.command()
def plant() -> None:
    """Plant canaries across the repo. (not implemented yet — M2)"""
    console.print("[yellow]plant: not implemented yet — see PLAN.md M2.[/yellow]")
    raise typer.Exit(code=2)


@app.command("list")
def list_() -> None:
    """List planted canaries. (not implemented yet — M3)"""
    console.print("[yellow]list: not implemented yet — see PLAN.md M3.[/yellow]")
    raise typer.Exit(code=2)


@app.command()
def check() -> None:
    """Scan for evidence a canary fired. (not implemented yet — M4)"""
    console.print("[yellow]check: not implemented yet — see PLAN.md M4.[/yellow]")
    raise typer.Exit(code=2)


@app.command()
def uproot() -> None:
    """Remove planted canaries. (not implemented yet — M2)"""
    console.print("[yellow]uproot: not implemented yet — see PLAN.md M2.[/yellow]")
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
