"""Typer CLI entrypoint for canary-cage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .canaries import DocstringCanary, ManifestCanary, MarkdownCanary, TodoCanary
from .config import (
    CONFIG_FILE_NAME,
    PRESETS,
    PlantFilter,
    config_path,
    load_config,
    write_default_config,
)
from .diff import (
    VERDICT_FIRED,
    VERDICT_INTACT,
    VERDICT_MUTATED,
    VERDICT_REMOVED,
    compute_diff,
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
    "manifest": ManifestCanary,
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
    help="Canary type to plant: markdown, docstring, todo, manifest, or 'all'.",
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
    arm_at: str | None = typer.Option(
        None,
        "--arm-at",
        help=(
            "ISO-8601 timestamp (UTC if no tz) before which planted canaries "
            "stay dormant. Overrides any [canary] arm_at in canary.toml."
        ),
    ),
) -> None:
    """Plant canaries across the repo and record them in state."""

    root = _resolve_root(root)
    try:
        config = load_config(root)
    except (ValueError, OSError) as exc:
        console.print(f"[red]bad {CONFIG_FILE_NAME}: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    plant_filter = PlantFilter(config)

    arm_dt: datetime | None = None
    if arm_at is not None:
        try:
            arm_dt = _parse_iso_utc(arm_at)
        except ValueError as exc:
            console.print(f"[red]bad --arm-at: {exc}[/red]")
            raise typer.Exit(code=2) from exc
    elif config.arm_at is not None:
        arm_dt = config.arm_at
        if arm_dt.tzinfo is None:
            arm_dt = arm_dt.replace(tzinfo=UTC)

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

    if arm_dt is not None:
        for c in newly_planted:
            c.armed_at = arm_dt

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
    table.add_column("status", style="yellow")
    now = datetime.now(UTC)
    for c in sorted(state.canaries, key=lambda x: (x.type, x.path)):
        if c.armed_at is None:
            status = "armed"
        elif c.is_armed(now):
            status = f"armed ({c.armed_at.strftime('%Y-%m-%d %H:%M:%SZ')})"
        else:
            status = f"dormant → {c.armed_at.strftime('%Y-%m-%d %H:%M:%SZ')}"
        table.add_row(
            c.id,
            c.type,
            c.path,
            c.planted_at.strftime("%Y-%m-%d %H:%M:%SZ"),
            status,
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


_VERDICT_STYLE = {
    VERDICT_INTACT: "green",
    VERDICT_MUTATED: "yellow",
    VERDICT_REMOVED: "red",
    VERDICT_FIRED: "bold red",
}


@app.command()
def diff(
    root: Path | None = _ROOT_OPTION,
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Limit diff to canaries whose file changed in git since <ref>.",
    ),
) -> None:
    """Show what changed since plant, attributed by canary type."""

    import json as _json

    root = _resolve_root(root)
    report = compute_diff(root, since=since)

    if json_out:
        console.print_json(_json.dumps(report.to_dict()))
        bad = (VERDICT_MUTATED, VERDICT_REMOVED, VERDICT_FIRED)
        if any(d.verdict in bad for d in report.diffs):
            raise typer.Exit(code=1)
        return

    if not report.diffs:
        if report.total_planted == 0:
            console.print("[yellow]no canaries planted — nothing to diff.[/yellow]")
        else:
            console.print(
                f"🐤 [green]no diff — {report.total_planted} canar"
                f"{'y' if report.total_planted == 1 else 'ies'} unchanged"
                + (f" since {since}" if since else "")
                + ".[/green]"
            )
        return

    if all(d.verdict == VERDICT_INTACT for d in report.diffs):
        n = len(report.diffs)
        console.print(
            f"🐤 [green]no diff — {n} canar{'y' if n == 1 else 'ies'} unchanged"
            + (f" since {since}" if since else "")
            + ".[/green]"
        )
        return

    grouped: dict[str, list] = {}
    for d in report.diffs:
        grouped.setdefault(d.canary_type, []).append(d)

    for ctype, items in sorted(grouped.items()):
        table = Table(title=f"🐤 {ctype} ({len(items)})")
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("path", style="green")
        table.add_column("verdict")
        table.add_column("detail", style="dim")
        for d in items:
            style = _VERDICT_STYLE.get(d.verdict, "white")
            table.add_row(
                d.canary_id,
                d.path,
                f"[{style}]{d.verdict}[/{style}]",
                d.detail,
            )
        console.print(table)

    summary = report.summary()
    _order = (VERDICT_INTACT, VERDICT_MUTATED, VERDICT_REMOVED, VERDICT_FIRED)
    parts = [f"{v}={summary.get(v, 0)}" for v in _order]
    console.print("  ".join(parts))
    if any(
        d.verdict in (VERDICT_MUTATED, VERDICT_REMOVED, VERDICT_FIRED)
        for d in report.diffs
    ):
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
def arm(
    canary_id: str = typer.Argument(..., help="Canary id (or prefix) to arm immediately."),
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Manually arm a dormant (time-bomb) canary right now."""

    root = _resolve_root(root)
    state = load_state(root)
    matches = [c for c in state.canaries if c.id == canary_id or c.id.startswith(canary_id)]
    if not matches:
        console.print(f"[red]no canary matches id {canary_id!r}[/red]")
        raise typer.Exit(code=1)
    if len(matches) > 1:
        ids = ", ".join(c.id for c in matches)
        console.print(f"[red]ambiguous id {canary_id!r} matches: {ids}[/red]")
        raise typer.Exit(code=2)
    target = matches[0]
    if target.armed_at is None or target.is_armed():
        console.print(f"[yellow]canary {target.id} is already armed.[/yellow]")
        return
    target.armed_at = datetime.now(UTC)
    save_state(root, state)
    console.print(f"🔔 armed canary {target.id} ({target.type} → {target.path}).")


def _parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp, defaulting naive values to UTC."""
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"not a valid ISO-8601 timestamp: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


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

