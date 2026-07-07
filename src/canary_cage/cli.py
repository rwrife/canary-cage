"""Typer CLI entrypoint for canary-cage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .bait_tools import (
    add_bait_tool,
    bait_tools_path,
    list_bait_tools,
    remove_bait_tool,
)
from .canaries import (
    DocstringCanary,
    ManifestCanary,
    MarkdownCanary,
    ReverseCanary,
    TodoCanary,
)
from .config import (
    CONFIG_FILE_NAME,
    PRESETS,
    PlantFilter,
    config_path,
    load_config,
    write_default_config,
)
from .dashboard import run_dashboard
from .diff import (
    VERDICT_FIRED,
    VERDICT_INTACT,
    VERDICT_MUTATED,
    VERDICT_REMOVED,
    compute_diff,
)
from .fingerprint import (
    Fingerprinter,
    context_from_canary,
    identify_for_canary_id,
)
from .honey import (
    DEFAULT_LABEL as HONEY_DEFAULT_LABEL,
)
from .honey import (
    HoneyError,
    check_honey_fires,
    list_honey,
    plant_honey_issue,
    plant_honey_pr,
    uproot_honey,
)
from .mcp import serve as serve_mcp
from .precommit import check_staged, install_hook
from .scanner import scan, scan_outputs
from .state import load_state, save_state, state_path

app = typer.Typer(
    name="canary",
    help="🐤 canary-cage — plant prompt-injection tripwires and catch agentjacking in the act.",
    no_args_is_help=True,
    add_completion=False,
)

honey_app = typer.Typer(
    name="honey",
    help="🍯 Plant canaries into GitHub issues/PRs via `gh` (issue #28).",
    no_args_is_help=True,
)
app.add_typer(honey_app, name="honey")

bait_app = typer.Typer(
    name="bait-tool",
    help="🪝 Manage MCP bait tools — the tool-hijacking honeypot (issue #35).",
    no_args_is_help=True,
)
app.add_typer(bait_app, name="bait-tool")

console = Console()

_CANARY_REGISTRY = {
    "markdown": MarkdownCanary,
    "docstring": DocstringCanary,
    "todo": TodoCanary,
    "manifest": ManifestCanary,
    "reverse": ReverseCanary,
}

# Public alias for external consumers (issue #33). Kept in sync with
# ``_CANARY_REGISTRY`` — the underscored name predates the public API.
CANARY_REGISTRY = _CANARY_REGISTRY


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

_SCAN_OUTPUTS_OPTION = typer.Option(
    None,
    "--scan-outputs",
    help=(
        "Also grep this path/glob for reverse-canary tokens. "
        "May be given multiple times."
    ),
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

    state = load_state(root)  # reload to include honey if not already loaded
    if state.honey:
        htable = Table(title=f"🍯 honey ({len(state.honey)})")
        htable.add_column("id", style="cyan", no_wrap=True)
        htable.add_column("kind", style="magenta")
        htable.add_column("repo", style="green")
        htable.add_column("#", style="yellow", no_wrap=True)
        htable.add_column("url", style="dim")
        for h in sorted(state.honey, key=lambda x: (x.kind, x.repo, x.github_id)):
            htable.add_row(h.id, h.kind, h.repo, str(h.github_id), h.url)
        console.print(htable)


@honey_app.command("issue")
def honey_issue_cmd(
    repo: str = typer.Option(..., "--repo", help="owner/name"),
    title: str = typer.Option(..., "--title", help="Issue title."),
    body: str = typer.Option("", "--body", help="Optional body prose; canary is appended."),
    label: str = typer.Option(
        HONEY_DEFAULT_LABEL, "--label", help="Label to apply (pass '' to skip)."
    ),
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Create a labeled honey issue with an embedded canary."""

    root = _resolve_root(root)
    try:
        art = plant_honey_issue(root, repo=repo, title=title, body=body, label=label)
    except HoneyError as exc:
        console.print(f"[red]honey issue failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"🍯 planted issue [cyan]{art.id}[/cyan] → {art.url}")


@honey_app.command("pr")
def honey_pr_cmd(
    repo: str = typer.Option(..., "--repo", help="owner/name"),
    branch: str = typer.Option(..., "--branch", help="Head branch (must be pushed)."),
    title: str = typer.Option(..., "--title", help="PR title."),
    body: str = typer.Option("", "--body", help="Optional body prose; canary is appended."),
    base: str = typer.Option("main", "--base", help="Base branch."),
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Open a draft honey PR with an embedded canary body."""

    root = _resolve_root(root)
    try:
        art = plant_honey_pr(
            root, repo=repo, branch=branch, title=title, body=body, base=base
        )
    except HoneyError as exc:
        console.print(f"[red]honey pr failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"🍯 planted PR [cyan]{art.id}[/cyan] → {art.url}")


@honey_app.command("list")
def honey_list_cmd(root: Path | None = _ROOT_OPTION) -> None:
    """List every planted honey artifact."""

    root = _resolve_root(root)
    artifacts = list_honey(root)
    if not artifacts:
        console.print("[yellow]no honey artifacts — run `canary honey issue|pr`.[/yellow]")
        return
    table = Table(title=f"🍯 honey ({len(artifacts)})")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("kind", style="magenta")
    table.add_column("repo", style="green")
    table.add_column("#", style="yellow", no_wrap=True)
    table.add_column("url", style="dim")
    for h in artifacts:
        table.add_row(h.id, h.kind, h.repo, str(h.github_id), h.url)
    console.print(table)


@honey_app.command("check")
def honey_check_cmd(
    root: Path | None = _ROOT_OPTION,
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Re-fetch honey artifacts via `gh` and report mutations/comments."""

    import json as _json

    root = _resolve_root(root)
    try:
        fires = check_honey_fires(root)
    except HoneyError as exc:
        console.print(f"[red]honey check failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_out:
        payload = [
            {"artifact_id": f.artifact_id, "kind": f.kind, "detail": f.detail}
            for f in fires
        ]
        console.print_json(_json.dumps(payload))
        if fires:
            raise typer.Exit(code=1)
        return

    if not fires:
        console.print("🐤 [green]no honey fires — all quiet.[/green]")
        return
    table = Table(title=f"🍯 honey fires ({len(fires)})")
    table.add_column("artifact", style="cyan")
    table.add_column("kind", style="red")
    table.add_column("detail", style="dim")
    for f in fires:
        table.add_row(f.artifact_id, f.kind, f.detail)
    console.print(table)
    raise typer.Exit(code=1)


@app.command()
def check(
    root: Path | None = _ROOT_OPTION,
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
    scan_outputs_paths: list[str] | None = _SCAN_OUTPUTS_OPTION,
) -> None:
    """Scan for evidence a canary fired and fire beacons for each hit."""

    import json as _json

    root = _resolve_root(root)
    fires = scan(root)
    if scan_outputs_paths:
        fires.extend(scan_outputs(root, scan_outputs_paths))

    # Attribute each fire to a likely agent (additive — never breaks existing
    # consumers; `attributed_to` is just a new optional field).
    fp = Fingerprinter(root=root)
    state = load_state(root)
    canaries_by_id = {c.id: c for c in state.canaries}
    attributions: list[dict[str, object] | None] = []
    for rec in fires:
        canary = canaries_by_id.get(rec.canary_id)
        if canary is None:
            attributions.append(None)
            continue
        ctx = context_from_canary(root, canary)
        report = fp.identify(ctx)
        attributions.append(report.to_dict() if report.candidates else None)

    if json_out:
        payload = {
            "schema_version": 1,
            "root": str(root),
            "fires": [
                {
                    "canary_id": rec.canary_id,
                    "canary_type": rec.canary_type,
                    "source": rec.source,
                    "detail": rec.detail,
                    "path": rec.path,
                    "attributed_to": attr,
                }
                for rec, attr in zip(fires, attributions, strict=False)
            ],
        }
        console.print_json(_json.dumps(payload))
        if fires:
            raise typer.Exit(code=1)
        return

    if not fires:
        console.print("🐤 [green]all canaries singing — no fires detected.[/green]")
        return

    table = Table(title=f"🚨 {len(fires)} canary fire(s) detected")
    table.add_column("canary_id", style="cyan", no_wrap=True)
    table.add_column("type", style="magenta")
    table.add_column("source", style="yellow")
    table.add_column("attributed_to", style="blue")
    table.add_column("detail", style="red")
    for rec, attr in zip(fires, attributions, strict=False):
        if attr and attr.get("top"):
            top = attr["top"]
            attributed = f"{top['agent']} ({top['confidence']:.2f})"
        else:
            attributed = "—"
        table.add_row(rec.canary_id, rec.canary_type, rec.source, attributed, rec.detail)
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
def fingerprint(
    canary_id: str = typer.Argument(..., help="id of a planted (or recently fired) canary"),
    root: Path | None = _ROOT_OPTION,
    user_agent: str | None = typer.Option(
        None, "--user-agent", help="Optional User-Agent string (e.g. from a webhook hit)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit JSON instead of a Rich table."
    ),
) -> None:
    """Explain a single fire — which agent likely tripped this canary?"""

    import json as _json

    root = _resolve_root(root)
    result = identify_for_canary_id(root, canary_id, user_agent=user_agent)
    if result is None:
        console.print(f"[red]no canary with id {canary_id!r} — nothing to fingerprint.[/red]")
        raise typer.Exit(code=2)
    ctx, report = result

    if json_out:
        payload = {
            "canary_id": ctx.canary_id,
            "canary_type": ctx.canary_type,
            "path": ctx.path,
            "commit_sha": ctx.commit_sha,
            "commit_author": ctx.commit_author,
            "user_agent": ctx.user_agent,
            "attributed_to": report.to_dict(),
        }
        console.print_json(_json.dumps(payload))
        return

    if not report.candidates:
        console.print(
            f"🔍 [yellow]no agent matched any rule for {canary_id} — "
            "could be a human, an unsupported agent, or a clean canary.[/yellow]"
        )
        if ctx.commit_author:
            console.print(f"  last commit author on this path: {ctx.commit_author}")
        return

    table = Table(title=f"🔍 fingerprint for {canary_id}")
    table.add_column("rank", style="dim", no_wrap=True)
    table.add_column("agent", style="cyan")
    table.add_column("confidence", style="green")
    table.add_column("matched signals", style="yellow")
    for i, cand in enumerate(report.candidates, start=1):
        table.add_row(
            str(i),
            cand.display,
            f"{cand.confidence:.2f}",
            ", ".join(cand.signals),
        )
    console.print(table)
    if ctx.commit_author or ctx.commit_sha:
        bits = []
        if ctx.commit_sha:
            bits.append(f"commit={ctx.commit_sha[:12]}")
        if ctx.commit_author:
            bits.append(f"author={ctx.commit_author}")
        console.print("  " + "  ".join(bits))


@app.command()
def uproot(
    root: Path | None = _ROOT_OPTION,
    honey: bool = typer.Option(
        False,
        "--honey",
        help="Also clean up honey issues/PRs planted via `canary honey`.",
    ),
    honey_mode: str = typer.Option(
        "close",
        "--honey-mode",
        help="How to dispose of honey artifacts: close | delete | strip.",
    ),
) -> None:
    """Remove every planted canary and clear state."""

    root = _resolve_root(root)
    state = load_state(root)
    if not state.canaries and not honey:
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

    honey_removed = 0
    if honey:
        if honey_mode not in ("close", "delete", "strip"):
            console.print(
                f"[red]invalid --honey-mode {honey_mode!r} (expected close|delete|strip)[/red]"
            )
            raise typer.Exit(code=2)
        try:
            honey_removed = uproot_honey(root, mode=honey_mode)  # type: ignore[arg-type]
        except HoneyError as exc:
            console.print(f"[red]honey uproot failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc

    # Reload state so we don't clobber honey cleanup above.
    fresh = load_state(root)
    fresh.canaries = []
    save_state(root, fresh)
    console.print(
        f"🧹 uprooted [bold]{removed}[/bold] canar"
        f"{'y' if removed == 1 else 'ies'}."
    )

    if honey:
        console.print(
            f"🍯 uprooted [bold]{honey_removed}[/bold] honey artifact"
            f"{'' if honey_removed == 1 else 's'} ({honey_mode})."
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


@bait_app.command("list")
def bait_list_cmd(
    root: Path | None = _ROOT_OPTION,
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List every registered bait tool (built-ins + user-defined)."""

    import json as _json

    root = _resolve_root(root)
    tools = list_bait_tools(root)
    if json_out:
        console.print_json(_json.dumps([t.to_dict() for t in tools]))
        return
    if not tools:
        console.print("[yellow]no bait tools registered.[/yellow]")
        return
    table = Table(title=f"🪝 bait tools ({len(tools)})")
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("description", style="green")
    for t in tools:
        table.add_row(t.name, t.description)
    console.print(table)
    console.print(
        f"  user-defined bait tools stored at [dim]{bait_tools_path(root)}[/dim]"
    )


@bait_app.command("add")
def bait_add_cmd(
    name: str = typer.Option(..., "--name", help="Tool name (letters, digits, _, -, .)."),
    description: str = typer.Option(..., "--description", help="What the tool 'does'."),
    decoy_return: str | None = typer.Option(
        None,
        "--decoy-return",
        help=(
            "JSON object the bait tool returns to the calling agent. "
            "Defaults to '{}'."
        ),
    ),
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Register a user-defined bait tool."""

    import json as _json

    root = _resolve_root(root)
    decoy: dict[str, object] = {}
    if decoy_return is not None:
        try:
            parsed = _json.loads(decoy_return)
        except _json.JSONDecodeError as exc:
            console.print(f"[red]bad --decoy-return JSON: {exc}[/red]")
            raise typer.Exit(code=2) from exc
        if not isinstance(parsed, dict):
            console.print("[red]--decoy-return must decode to a JSON object.[/red]")
            raise typer.Exit(code=2)
        decoy = parsed
    try:
        tool = add_bait_tool(root, name=name, description=description, decoy_return=decoy)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"🪝 registered bait tool [cyan]{tool.name}[/cyan] → {bait_tools_path(root)}"
    )


@bait_app.command("remove")
def bait_remove_cmd(
    name: str = typer.Option(..., "--name", help="Bait-tool name to remove."),
    root: Path | None = _ROOT_OPTION,
) -> None:
    """Remove a user-defined bait tool. Built-ins are immutable."""

    root = _resolve_root(root)
    try:
        removed = remove_bait_tool(root, name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not removed:
        console.print(f"[yellow]no user-defined bait tool named {name!r}.[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"🧹 removed bait tool [cyan]{name}[/cyan].")


@app.command()
def dashboard(
    root: Path | None = _ROOT_OPTION,
    days: int = typer.Option(
        7,
        "--days",
        "-d",
        min=1,
        help="How many trailing UTC days to include in the heatmap.",
    ),
    refresh: float = typer.Option(
        2.0,
        "--refresh",
        "-r",
        min=0.1,
        help="Seconds between live-refresh frames (ignored with --once).",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Render a single frame and exit (screenshots, CI, tests).",
    ),
) -> None:
    """🐦 Live TUI dashboard of planted / fired / silent canaries (issue #34).

    Reads every registered beacon reader (file + log beacons out of the box)
    and renders a heatmap, a recent-fires table, and a summary tile. Pass
    ``--once`` for a static single-frame render that plays well with CI and
    ``rich.console.Console.capture()``.
    """

    root = _resolve_root(root)
    try:
        run_dashboard(root, days=days, refresh=refresh, once=once, console=console)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None


if __name__ == "__main__":
    app()

