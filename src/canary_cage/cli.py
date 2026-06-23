"""Typer CLI entrypoint for canary-cage."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .canaries import DocstringCanary, MarkdownCanary, TodoCanary
from .config import (
    CONFIG_FILE_NAME,
    PRESETS,
    PlantFilter,
    config_path,
    load_config,
    write_default_config,
)
from .mcp import serve as serve_mcp
from .precommit import check_staged, install_hook
from .scanner import scan
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
def init(
    root: Path | None = _ROOT_OPTION,
    preset: str | None = typer.Option(
        None,
        "--preset",
        "-p",
        help="Seed the config with a named preset (minimal, paranoid, chaotic-good).",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing canary.toml."
    ),
) -> None:
    """Write a default ``canary.toml`` config to the repo root."""

    root = _resolve_root(root)
    if preset is not None and preset not in PRESETS:
        known = ", ".join(sorted(PRESETS))
        console.print(f"[red]unknown preset: {preset!r} (known: {known})[/red]")
        raise typer.Exit(code=2)
    try:
        path = write_default_config(root, overwrite=force)
    except FileExistsError:
        console.print(
            f"[yellow]{CONFIG_FILE_NAME} already exists at {config_path(root)} — "
            "pass --force to overwrite.[/yellow]"
        )
        raise typer.Exit(code=1) from None
    if preset is not None:
        text = path.read_text(encoding="utf-8")
        needle = f'# preset = "{preset}"'
        if needle in text:
            text = text.replace(needle, f'preset = "{preset}"', 1)
            path.write_text(text, encoding="utf-8")
    console.print(f"🐤 wrote {path}" + (f" (preset: {preset})" if preset else ""))


@app.command()
def plant(
    type: str = _TYPE_OPTION,
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Plant canaries across the repo and record them in state."""

    root = _resolve_root(root)
    try:
        config = load_config(root)
    except (ValueError, OSError) as exc:
        console.print(f"[red]bad {CONFIG_FILE_NAME}: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    plant_filter = PlantFilter(config)

    if type == "all":
        types = tuple(config.types)
    elif type in _CANARY_REGISTRY:
        if type not in config.types:
            console.print(
                f"[yellow]warning: {type!r} not in canary.toml [canary].types — "
                "planting anyway because it was requested explicitly.[/yellow]"
            )
        types = (type,)
    else:
        known = ", ".join((*sorted(_CANARY_REGISTRY), "all"))
        console.print(f"[red]unknown canary type: {type!r} (known: {known})[/red]")
        raise typer.Exit(code=2)

    state = load_state(root)
    newly_planted = []
    for t in types:
        newly_planted.extend(_CANARY_REGISTRY[t]().plant(root, plant_filter))

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
def check(
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Scan for evidence a canary fired and fire beacons for each hit."""

    root = _resolve_root(root)
    fires = scan(root)
    if not fires:
        console.print("🐤 [green]all canaries singing — no fires detected.[/green]")
        return

    table = Table(title=f"🚨 {len(fires)} canary fire(s) detected")
    table.add_column("canary_id", style="cyan", no_wrap=True)
    table.add_column("type", style="magenta")
    table.add_column("source", style="yellow")
    table.add_column("detail", style="red")
    for rec in fires:
        table.add_row(rec.canary_id, rec.canary_type, rec.source, rec.detail)
    console.print(table)
    raise typer.Exit(code=1)


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


@app.command()
def precommit(
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Block commits that remove canaries or leak fired-beacon files.

    Designed to be invoked from ``.git/hooks/pre-commit``. Exits 0 when
    the staged diff is clean, 1 when violations are found.
    """

    root = _resolve_root(root)
    violations = check_staged(root)
    if not violations:
        console.print("🐤 [green]canary-cage: staged diff looks clean.[/green]")
        return

    table = Table(title=f"🚫 {len(violations)} canary-cage pre-commit violation(s)")
    table.add_column("kind", style="red", no_wrap=True)
    table.add_column("path", style="green")
    table.add_column("detail", style="yellow")
    for v in violations:
        table.add_row(v.kind, v.path, v.detail)
    console.print(table)
    raise typer.Exit(code=1)


@app.command("install-hook")
def install_hook_cmd(
    root: Path | None = _ROOT_OPTION,
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing pre-commit hook."
    ),
) -> None:
    """Install a ``.git/hooks/pre-commit`` that runs ``canary precommit``."""

    root = _resolve_root(root)
    try:
        path = install_hook(root, force=force)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    except FileExistsError as exc:
        console.print(
            f"[yellow]pre-commit hook already exists at {exc} — pass --force to overwrite.[/yellow]"
        )
        raise typer.Exit(code=1) from None
    console.print(f"🪵 installed pre-commit hook at {path}")


@app.command()
def mcp(
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Run the MCP server (JSON-RPC over stdio).

    Trusted agents can connect to discover planted canaries and avoid
    tripping them. See README for the wire protocol.
    """

    root = _resolve_root(root)
    try:
        serve_mcp(root)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None


if __name__ == "__main__":
    app()

