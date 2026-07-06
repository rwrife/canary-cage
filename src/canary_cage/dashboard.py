"""Heatmap TUI dashboard for canary-cage (issue #34).

The dashboard reads fires that have been persisted by the file and log
beacons and renders three Rich panels:

* a heatmap of fires per canary type over the last ``N`` days,
* a recent-fires table with fingerprint / agent guesses when available, and
* a summary of planted vs fired vs silent canaries.

The reader layer is intentionally pluggable so future beacons (chat, otel,
webhook, ...) can register a reader by name via
:func:`register_beacon_reader`. The bundled readers cover the two beacons
that persist locally today: :data:`file_beacon_reader` and
:data:`log_beacon_reader`.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import ValidationError
from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .beacons.base import BeaconRecord
from .beacons.file import fired_dir
from .beacons.log import log_path
from .fingerprint import identify_for_canary_id
from .state import STATE_DIR_NAME, load_state

# ---------------------------------------------------------------------------
# Pluggable beacon readers
# ---------------------------------------------------------------------------


BeaconReader = Callable[[Path], list[BeaconRecord]]
"""Reader signature: given the cage root, return every fire the beacon has
persisted locally. Readers must be side-effect free."""


_READERS: dict[str, BeaconReader] = {}


def register_beacon_reader(name: str, reader: BeaconReader) -> None:
    """Register (or overwrite) a beacon reader under ``name``.

    Names should match the corresponding ``Beacon.name`` where possible so
    UI code can label rows consistently.
    """

    _READERS[name] = reader


def registered_readers() -> dict[str, BeaconReader]:
    """Return a copy of the current reader registry (helpful for tests)."""

    return dict(_READERS)


def _parse_record(payload: dict) -> BeaconRecord | None:
    try:
        return BeaconRecord.model_validate(payload)
    except ValidationError:
        return None


def file_beacon_reader(root: Path) -> list[BeaconRecord]:
    """Read every fire persisted by :class:`~canary_cage.beacons.FileBeacon`.

    The file beacon writes one JSON document per canary id, overwriting on
    each fire, so this reader yields at most one record per planted canary.
    """

    directory = fired_dir(root)
    if not directory.exists():
        return []
    fires: list[BeaconRecord] = []
    for entry in sorted(directory.glob("*.json")):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        record = _parse_record(data)
        if record is not None:
            fires.append(record)
    return fires


def log_beacon_reader(root: Path) -> list[BeaconRecord]:
    """Read every fire persisted by :class:`~canary_cage.beacons.LogBeacon`.

    The log beacon is append-only JSON-lines, so this yields the full
    history — including repeat fires for the same canary id.
    """

    path = log_path(root)
    if not path.exists():
        return []
    fires: list[BeaconRecord] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        record = _parse_record(payload)
        if record is not None:
            fires.append(record)
    return fires


# Register the built-in readers eagerly so callers don't have to.
register_beacon_reader("file", file_beacon_reader)
register_beacon_reader("log", log_beacon_reader)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _record_key(record: BeaconRecord) -> tuple[str, str, str]:
    """Deduplication key across beacon readers.

    Different beacons persist the *same* firing event under different
    on-disk formats, so we key by (canary_id, source, detected_at ISO
    string with second precision) to collapse duplicates without losing
    repeat fires from the append-only log.
    """

    detected = _as_utc(record.detected_at).replace(microsecond=0).isoformat()
    return (record.canary_id, record.source, detected)


def collect_fires(
    root: Path,
    *,
    days: int | None = None,
    now: datetime | None = None,
    readers: Iterable[str] | None = None,
) -> list[BeaconRecord]:
    """Collect + dedupe fires from every registered beacon reader.

    Records are returned sorted newest → oldest. When ``days`` is set,
    records older than the cutoff are dropped. ``readers`` narrows the
    set of readers consulted (defaults to every registered reader).
    """

    if days is not None and days <= 0:
        raise ValueError("days must be >= 1")

    now_ts = _as_utc(now) if now is not None else datetime.now(UTC)
    cutoff = now_ts - timedelta(days=days) if days is not None else None
    selected = list(readers) if readers is not None else list(_READERS.keys())

    seen: dict[tuple[str, str, str], BeaconRecord] = {}
    for name in selected:
        reader = _READERS.get(name)
        if reader is None:
            continue
        try:
            for record in reader(root):
                if cutoff is not None and _as_utc(record.detected_at) < cutoff:
                    continue
                key = _record_key(record)
                # Log beacon wins over file beacon when both saw the same
                # event, because the log preserves history; but any first
                # write is kept for reader-only setups.
                if key not in seen:
                    seen[key] = record
        except Exception:  # pragma: no cover - defensive
            continue

    ordered = sorted(seen.values(), key=lambda r: _as_utc(r.detected_at), reverse=True)
    return ordered


def fires_per_type(records: Iterable[BeaconRecord]) -> dict[str, int]:
    """Count fires grouped by canary type."""

    tally: dict[str, int] = defaultdict(int)
    for record in records:
        tally[record.canary_type] += 1
    return dict(tally)


def fires_per_day(
    records: Iterable[BeaconRecord],
    *,
    days: int,
    now: datetime | None = None,
) -> dict[date, int]:
    """Count fires per UTC day for the trailing ``days`` window.

    The returned mapping is dense: every day in the window is present, even
    if the count is zero, ordered oldest → newest. That keeps the heatmap
    layout stable across renders.
    """

    if days <= 0:
        raise ValueError("days must be >= 1")

    now_ts = _as_utc(now) if now is not None else datetime.now(UTC)
    today = now_ts.date()
    buckets: dict[date, int] = {
        today - timedelta(days=offset): 0 for offset in reversed(range(days))
    }

    for record in records:
        day = _as_utc(record.detected_at).date()
        if day in buckets:
            buckets[day] += 1
    return buckets


def fires_per_type_per_day(
    records: Iterable[BeaconRecord],
    *,
    days: int,
    now: datetime | None = None,
) -> dict[str, dict[date, int]]:
    """Return a two-level breakdown: ``{canary_type: {day: count}}``.

    Each canary type maps to a *dense* day mapping (same shape as
    :func:`fires_per_day`) so the heatmap can be rendered without extra
    bookkeeping.
    """

    if days <= 0:
        raise ValueError("days must be >= 1")

    now_ts = _as_utc(now) if now is not None else datetime.now(UTC)
    today = now_ts.date()
    day_keys = [today - timedelta(days=offset) for offset in reversed(range(days))]

    materialised = list(records)
    types = sorted({record.canary_type for record in materialised})

    result: dict[str, dict[date, int]] = {
        canary_type: {day: 0 for day in day_keys} for canary_type in types
    }
    for record in materialised:
        day = _as_utc(record.detected_at).date()
        if day not in result[record.canary_type]:
            continue
        result[record.canary_type][day] += 1
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_HEATMAP_SHADES: tuple[tuple[int, str], ...] = (
    (0, "grey23"),
    (1, "grey42"),
    (3, "yellow3"),
    (7, "orange3"),
    (15, "red3"),
    (999999, "red1"),
)


def _shade_for(count: int) -> str:
    for threshold, colour in _HEATMAP_SHADES:
        if count <= threshold:
            return colour
    return _HEATMAP_SHADES[-1][1]


def _format_relative(now: datetime, when: datetime) -> str:
    delta = now - _as_utc(when)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


@dataclass(frozen=True)
class DashboardSummary:
    """Numbers used by the summary panel + tests."""

    planted: int
    fired_canary_ids: int
    silent: int
    total_fires: int
    beacons_active: int


def summarise(
    root: Path,
    records: Iterable[BeaconRecord],
) -> DashboardSummary:
    """Compute the summary tile numbers from planted state + fires."""

    state = load_state(root)
    planted_ids = {c.id for c in state.canaries}
    fires = list(records)
    fired_ids = {record.canary_id for record in fires}
    fired_planted = fired_ids & planted_ids
    silent = len(planted_ids - fired_ids)
    beacons = {reader_name for reader_name, reader in _READERS.items() if reader(root)}
    return DashboardSummary(
        planted=len(planted_ids),
        fired_canary_ids=len(fired_planted) or len(fired_ids),
        silent=silent,
        total_fires=len(fires),
        beacons_active=len(beacons),
    )


def _empty_state_panel(days: int) -> Panel:
    body = Text.assemble(
        ("🐤 no fires yet — the cage is quiet.\n\n", "bold green"),
        (f"Watching the trailing {days} day", "dim"),
        ("s" if days != 1 else "", "dim"),
        (
            " from every registered beacon reader. Once a canary trips,"
            " it will show up here.\n\n",
            "dim",
        ),
        ("Tip: ", "bold"),
        ("run `canary check` from CI, and enable the file or log beacon"
         " on any workflow that reads your repo.", ""),
    )
    return Panel(
        Align.center(body, vertical="middle"),
        title="🐦 canary-cage dashboard",
        border_style="green",
    )


def build_heatmap(
    records: Iterable[BeaconRecord],
    *,
    days: int,
    now: datetime | None = None,
) -> Panel:
    """Render the per-type × per-day heatmap panel."""

    now_ts = _as_utc(now) if now is not None else datetime.now(UTC)
    breakdown = fires_per_type_per_day(list(records), days=days, now=now_ts)
    day_keys = list(next(iter(breakdown.values()), {}).keys())
    if not day_keys:
        # No fires at all — but caller decides how to handle empty state.
        day_keys = [now_ts.date() - timedelta(days=offset) for offset in reversed(range(days))]

    table = Table.grid(padding=(0, 1))
    table.add_column("type", justify="right", no_wrap=True, style="bold cyan")
    for day in day_keys:
        table.add_column(day.strftime("%m-%d"), justify="center", no_wrap=True)
    table.add_column("Σ", justify="right", style="bold")

    header = ["", *[day.strftime("%m-%d") for day in day_keys], "Σ"]
    table.add_row(*[Text(h, style="dim") for h in header])

    if not breakdown:
        table.add_row(
            Text("—", style="dim"),
            *[Text("·", style="grey42") for _ in day_keys],
            Text("0", style="dim"),
        )

    for canary_type in sorted(breakdown.keys()):
        row_cells: list[RenderableType] = [Text(canary_type, style="bold cyan")]
        row_total = 0
        for day in day_keys:
            count = breakdown[canary_type][day]
            row_total += count
            colour = _shade_for(count)
            glyph = str(count) if count else "·"
            row_cells.append(Text(glyph, style=f"bold {colour}"))
        row_cells.append(Text(str(row_total), style="bold"))
        table.add_row(*row_cells)

    return Panel(
        table,
        title=f"🔥 fires per canary type — last {days} day(s)",
        border_style="red",
    )


def build_recent_table(
    records: Iterable[BeaconRecord],
    *,
    limit: int = 12,
    now: datetime | None = None,
) -> Panel:
    """Render the recent-fires table panel (newest first)."""

    now_ts = _as_utc(now) if now is not None else datetime.now(UTC)
    materialised = list(records)[:limit]

    table = Table(expand=True, show_lines=False, header_style="bold magenta")
    table.add_column("id", no_wrap=True, style="cyan")
    table.add_column("type", no_wrap=True, style="green")
    table.add_column("when", no_wrap=True, style="yellow")
    table.add_column("source", no_wrap=True)
    table.add_column("agent", no_wrap=True, style="magenta")
    table.add_column("detail", overflow="fold")

    if not materialised:
        table.add_row(Text("—", style="dim"), "", "", "", "", Text("no fires", style="dim"))
    else:
        for record in materialised:
            agent = "?"
            try:
                identification = identify_for_canary_id(
                    Path.cwd(), record.canary_id, user_agent=None
                )
            except Exception:  # pragma: no cover - fingerprinter is best-effort
                identification = None
            if identification is not None:
                agent = cast(str, getattr(identification, "agent", None) or "?")
            table.add_row(
                record.canary_id,
                record.canary_type,
                _format_relative(now_ts, record.detected_at),
                record.source,
                agent,
                record.detail,
            )

    return Panel(table, title="🕒 recent fires", border_style="magenta")


def build_summary(
    summary: DashboardSummary,
    *,
    days: int,
) -> Panel:
    """Render the summary tile."""

    silent_style = "green" if summary.silent == 0 else "yellow"
    total_style = "green" if summary.total_fires == 0 else "red"

    body = Table.grid(padding=(0, 2))
    body.add_column(justify="right", style="bold")
    body.add_column(justify="left")
    body.add_row("planted", Text(str(summary.planted), style="bold cyan"))
    body.add_row("fired", Text(str(summary.fired_canary_ids), style=f"bold {total_style}"))
    body.add_row("silent", Text(str(summary.silent), style=f"bold {silent_style}"))
    body.add_row("total fires", Text(str(summary.total_fires), style=f"bold {total_style}"))
    body.add_row("beacons w/ data", Text(str(summary.beacons_active), style="bold blue"))
    body.add_row("window", Text(f"{days} day(s)", style="dim"))

    return Panel(body, title="📊 summary", border_style="blue")


def render_dashboard(
    root: Path,
    *,
    days: int = 7,
    now: datetime | None = None,
    readers: Iterable[str] | None = None,
    recent_limit: int = 12,
) -> Layout:
    """Return a fully populated Rich :class:`Layout` for the dashboard.

    Callers can print this directly (``--once``) or drive it inside a
    ``rich.live.Live`` loop.
    """

    if days <= 0:
        raise ValueError("days must be >= 1")

    now_ts = _as_utc(now) if now is not None else datetime.now(UTC)
    records = collect_fires(root, days=days, now=now_ts, readers=readers)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    layout["header"].update(
        Panel(
            Align.center(
                Text(
                    f"🐤 canary-cage dashboard — {STATE_DIR_NAME}"
                    f" @ {root}  |  window: {days}d",
                    style="bold",
                ),
            ),
            border_style="cyan",
        )
    )

    if not records:
        layout["body"].update(_empty_state_panel(days))
        return layout

    heatmap = build_heatmap(records, days=days, now=now_ts)
    recent = build_recent_table(records, limit=recent_limit, now=now_ts)
    summary = build_summary(summarise(root, records), days=days)

    body = layout["body"]
    body.split_row(Layout(name="left", ratio=2), Layout(name="right", ratio=1))
    body["left"].split_column(Layout(heatmap, name="heatmap"), Layout(recent, name="recent"))
    body["right"].update(Group(summary))
    return layout


def run_dashboard(
    root: Path,
    *,
    days: int = 7,
    refresh: float = 2.0,
    once: bool = False,
    max_frames: int | None = None,
    now_provider: Callable[[], datetime] | None = None,
    console=None,
) -> int:
    """Run the dashboard.

    Returns the number of frames rendered so tests can assert on it.
    """

    if days <= 0:
        raise ValueError("days must be >= 1")
    if refresh <= 0:
        raise ValueError("refresh must be > 0")

    from rich.console import Console  # imported here to keep module import cheap
    from rich.live import Live

    console = console or Console()
    now_provider = now_provider or (lambda: datetime.now(UTC))

    frames = 0
    if once or max_frames == 1:
        layout = render_dashboard(root, days=days, now=now_provider())
        console.print(layout)
        return 1

    with Live(console=console, refresh_per_second=max(1.0, 1.0 / refresh), screen=False) as live:
        while True:
            layout = render_dashboard(root, days=days, now=now_provider())
            live.update(layout)
            frames += 1
            if max_frames is not None and frames >= max_frames:
                break
            try:
                time.sleep(refresh)
            except KeyboardInterrupt:
                break
    return frames


__all__ = [
    "BeaconReader",
    "DashboardSummary",
    "build_heatmap",
    "build_recent_table",
    "build_summary",
    "collect_fires",
    "file_beacon_reader",
    "fires_per_day",
    "fires_per_type",
    "fires_per_type_per_day",
    "log_beacon_reader",
    "register_beacon_reader",
    "registered_readers",
    "render_dashboard",
    "run_dashboard",
    "summarise",
]
