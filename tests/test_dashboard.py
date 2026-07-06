"""Tests for the heatmap TUI dashboard (issue #34)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from canary_cage.beacons import BeaconRecord, FileBeacon, LogBeacon
from canary_cage.beacons.file import fired_dir
from canary_cage.beacons.log import log_path
from canary_cage.cli import app
from canary_cage.dashboard import (
    DashboardSummary,
    build_heatmap,
    build_recent_table,
    build_summary,
    collect_fires,
    file_beacon_reader,
    fires_per_day,
    fires_per_type,
    fires_per_type_per_day,
    log_beacon_reader,
    register_beacon_reader,
    registered_readers,
    render_dashboard,
    run_dashboard,
    summarise,
)
from canary_cage.state import CageState, PlantedCanary, save_state

runner = CliRunner()


NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def _plant(root: Path, records: list[BeaconRecord], *, use_log: bool = True) -> None:
    fb = FileBeacon()
    lb = LogBeacon()
    for record in records:
        fb.fire(root, record)
        if use_log:
            lb.fire(root, record)


def _make_record(**overrides) -> BeaconRecord:
    defaults = dict(
        canary_id="c-1",
        canary_type="markdown",
        source="working-tree",
        detail="drift",
        path="README.md",
        detected_at=NOW,
    )
    defaults.update(overrides)
    return BeaconRecord(**defaults)


# ---------------------------------------------------------------------------
# Beacon readers
# ---------------------------------------------------------------------------


def test_file_beacon_reader_empty(tmp_path: Path) -> None:
    assert file_beacon_reader(tmp_path) == []


def test_file_beacon_reader_yields_records(tmp_path: Path) -> None:
    _plant(tmp_path, [_make_record(canary_id="a"), _make_record(canary_id="b")])
    fires = file_beacon_reader(tmp_path)
    ids = sorted(record.canary_id for record in fires)
    assert ids == ["a", "b"]


def test_file_beacon_reader_ignores_junk(tmp_path: Path) -> None:
    directory = fired_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.json").write_text("this is not json", encoding="utf-8")
    (directory / "wrong-shape.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert file_beacon_reader(tmp_path) == []


def test_log_beacon_reader_history(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        [
            _make_record(canary_id="a", detected_at=NOW - timedelta(hours=2)),
            _make_record(canary_id="a", detected_at=NOW - timedelta(hours=1)),
            _make_record(canary_id="b", detected_at=NOW),
        ],
    )
    fires = log_beacon_reader(tmp_path)
    assert len(fires) == 3
    # newest → oldest ordering isn't guaranteed by the reader itself
    assert {record.canary_id for record in fires} == {"a", "b"}


def test_log_beacon_reader_skips_blank_and_bad_lines(tmp_path: Path) -> None:
    path = log_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(_make_record().model_dump(mode="json"))
    path.write_text(f"\n{good}\ngarbage\n{good}\n", encoding="utf-8")
    fires = log_beacon_reader(tmp_path)
    assert len(fires) == 2


# ---------------------------------------------------------------------------
# Reader registry
# ---------------------------------------------------------------------------


def test_registered_readers_includes_defaults() -> None:
    readers = registered_readers()
    assert "file" in readers
    assert "log" in readers


def test_register_beacon_reader_extends_registry(tmp_path: Path) -> None:
    marker = _make_record(canary_id="from-plugin")

    def fake_reader(_: Path) -> list[BeaconRecord]:
        return [marker]

    register_beacon_reader("__fake__", fake_reader)
    try:
        records = collect_fires(tmp_path, days=30, now=NOW)
        assert any(record.canary_id == "from-plugin" for record in records)
    finally:
        registered_readers().pop("__fake__", None)  # noop; keep original mutated too
        from canary_cage.dashboard import _READERS

        _READERS.pop("__fake__", None)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_collect_fires_dedupes_across_beacons(tmp_path: Path) -> None:
    record = _make_record(canary_id="dup", detected_at=NOW - timedelta(hours=1))
    _plant(tmp_path, [record])
    records = collect_fires(tmp_path, days=7, now=NOW)
    ids = [r.canary_id for r in records]
    assert ids.count("dup") == 1


def test_collect_fires_respects_days_window(tmp_path: Path) -> None:
    old = _make_record(canary_id="old", detected_at=NOW - timedelta(days=30))
    fresh = _make_record(canary_id="fresh", detected_at=NOW - timedelta(hours=2))
    _plant(tmp_path, [old, fresh])
    records = collect_fires(tmp_path, days=7, now=NOW)
    assert {r.canary_id for r in records} == {"fresh"}


def test_collect_fires_newest_first(tmp_path: Path) -> None:
    a = _make_record(canary_id="a", detected_at=NOW - timedelta(hours=5))
    b = _make_record(canary_id="b", detected_at=NOW - timedelta(hours=1))
    c = _make_record(canary_id="c", detected_at=NOW - timedelta(days=2))
    _plant(tmp_path, [a, b, c])
    records = collect_fires(tmp_path, days=7, now=NOW)
    assert [r.canary_id for r in records] == ["b", "a", "c"]


def test_fires_per_type_counts() -> None:
    records = [
        _make_record(canary_type="markdown"),
        _make_record(canary_type="markdown"),
        _make_record(canary_type="docstring"),
    ]
    assert fires_per_type(records) == {"markdown": 2, "docstring": 1}


def test_fires_per_day_is_dense_and_bucketed() -> None:
    records = [
        _make_record(detected_at=NOW),
        _make_record(detected_at=NOW - timedelta(days=1, hours=1)),
        _make_record(detected_at=NOW - timedelta(days=1, hours=2)),
    ]
    buckets = fires_per_day(records, days=3, now=NOW)
    assert len(buckets) == 3
    days = list(buckets.keys())
    assert days == sorted(days)  # oldest → newest
    assert buckets[NOW.date()] == 1
    assert buckets[(NOW - timedelta(days=1)).date()] == 2


def test_fires_per_type_per_day_has_stable_shape() -> None:
    records = [
        _make_record(canary_type="markdown", detected_at=NOW),
        _make_record(canary_type="todo", detected_at=NOW - timedelta(days=2)),
    ]
    breakdown = fires_per_type_per_day(records, days=3, now=NOW)
    assert set(breakdown.keys()) == {"markdown", "todo"}
    for day_map in breakdown.values():
        assert len(day_map) == 3
    assert breakdown["markdown"][NOW.date()] == 1
    assert breakdown["todo"][(NOW - timedelta(days=2)).date()] == 1


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summarise_uses_state_and_fires(tmp_path: Path) -> None:
    state = CageState(
        canaries=[
            PlantedCanary(id="p-1", type="markdown", path="README.md", marker="x"),
            PlantedCanary(id="p-2", type="docstring", path="foo.py", marker="y"),
            PlantedCanary(id="p-3", type="todo", path="bar.py", marker="z"),
        ]
    )
    save_state(tmp_path, state)

    fired = [
        _make_record(canary_id="p-1", canary_type="markdown"),
        _make_record(canary_id="p-1", canary_type="markdown"),  # repeat
        _make_record(canary_id="p-2", canary_type="docstring"),
    ]
    _plant(tmp_path, fired)
    records = collect_fires(tmp_path, days=7, now=NOW)
    summary = summarise(tmp_path, records)
    assert isinstance(summary, DashboardSummary)
    assert summary.planted == 3
    assert summary.silent == 1
    assert summary.total_fires >= 2  # at least the deduped events


# ---------------------------------------------------------------------------
# Rendering panels
# ---------------------------------------------------------------------------


def _render(renderable) -> str:
    console = Console(width=120, force_terminal=False, record=True)
    console.print(renderable)
    return console.export_text()


def test_build_heatmap_shows_type_and_day_headers() -> None:
    records = [
        _make_record(canary_type="markdown", detected_at=NOW),
        _make_record(canary_type="docstring", detected_at=NOW - timedelta(days=1)),
    ]
    text = _render(build_heatmap(records, days=3, now=NOW))
    assert "markdown" in text
    assert "docstring" in text
    assert NOW.strftime("%m-%d") in text


def test_build_recent_table_lists_ids() -> None:
    records = [
        _make_record(canary_id="rec-1", detail="first"),
        _make_record(canary_id="rec-2", detail="second", detected_at=NOW - timedelta(hours=5)),
    ]
    text = _render(build_recent_table(records, limit=5, now=NOW))
    assert "rec-1" in text
    assert "rec-2" in text
    assert "recent fires" in text.lower()


def test_build_recent_table_handles_empty() -> None:
    text = _render(build_recent_table([], now=NOW))
    assert "no fires" in text


def test_build_summary_renders_counts() -> None:
    summary = DashboardSummary(
        planted=3, fired_canary_ids=1, silent=2, total_fires=4, beacons_active=1
    )
    text = _render(build_summary(summary, days=5))
    assert "planted" in text
    assert "silent" in text
    assert "5 day" in text


# ---------------------------------------------------------------------------
# render_dashboard + run_dashboard
# ---------------------------------------------------------------------------


def test_render_dashboard_empty_state(tmp_path: Path) -> None:
    text = _render(render_dashboard(tmp_path, days=7, now=NOW))
    assert "no fires yet" in text
    assert "canary-cage dashboard" in text


def test_render_dashboard_populated_snapshot(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        [
            _make_record(canary_id="c-1", canary_type="markdown"),
            _make_record(
                canary_id="c-2",
                canary_type="docstring",
                detected_at=NOW - timedelta(days=1),
            ),
        ],
    )
    text = _render(render_dashboard(tmp_path, days=7, now=NOW))
    assert "canary-cage dashboard" in text
    assert "markdown" in text
    assert "c-1" in text
    assert "recent fires" in text.lower()


def test_run_dashboard_once_writes_frame(tmp_path: Path) -> None:
    console = Console(width=100, force_terminal=False, record=True)
    frames = run_dashboard(tmp_path, days=3, once=True, console=console)
    assert frames == 1
    assert "canary-cage dashboard" in console.export_text()


def test_run_dashboard_rejects_bad_args(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        run_dashboard(tmp_path, days=0, once=True)
    with pytest.raises(ValueError):
        run_dashboard(tmp_path, days=3, refresh=0, once=False, max_frames=1)


def test_run_dashboard_max_frames_loop(tmp_path: Path) -> None:
    console = Console(width=80, force_terminal=False, record=True)
    frames = run_dashboard(
        tmp_path,
        days=3,
        refresh=0.01,
        once=False,
        max_frames=2,
        console=console,
    )
    assert frames == 2


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_dashboard_once_empty(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dashboard", "--root", str(tmp_path), "--once"])
    assert result.exit_code == 0, result.stdout
    assert "canary-cage dashboard" in result.stdout


def test_cli_dashboard_once_populated(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        [
            _make_record(canary_id="cli-1"),
            _make_record(
                canary_id="cli-2",
                canary_type="todo",
                detected_at=NOW - timedelta(hours=6),
            ),
        ],
    )
    # Widen the runner's terminal so Rich doesn't ellipsis-truncate ids.
    result = runner.invoke(
        app,
        ["dashboard", "--root", str(tmp_path), "--once", "--days", "3"],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.stdout
    stdout = result.stdout
    assert "cli-1" in stdout
    assert "todo" in stdout
    assert "markdown" in stdout


def test_cli_dashboard_help_lists_flags() -> None:
    result = runner.invoke(app, ["dashboard", "--help"])
    assert result.exit_code == 0
    # Rich/Typer help wraps and colorizes output, so strip ANSI escape
    # sequences and normalize whitespace before asserting substrings.
    ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    cleaned = re.sub(r"\s+", " ", ansi_re.sub("", result.stdout))
    for flag in ("--days", "--refresh", "--once"):
        assert flag in cleaned
