"""Tests for the time-bomb canary feature (dormant until `armed_at`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from canary_cage.cli import app
from canary_cage.scanner import scan
from canary_cage.state import CageState, PlantedCanary, load_state, save_state

runner = CliRunner()


def _seed_markdown(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hello\n\nbody\n", encoding="utf-8")


def test_is_armed_default_none_means_always_armed() -> None:
    c = PlantedCanary(id="x", type="markdown", path="README.md", marker="abc")
    assert c.armed_at is None
    assert c.is_armed() is True


def test_is_armed_future_is_dormant() -> None:
    future = datetime.now(UTC) + timedelta(days=30)
    c = PlantedCanary(
        id="x", type="markdown", path="README.md", marker="abc", armed_at=future
    )
    assert c.is_armed() is False


def test_is_armed_past_is_armed() -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    c = PlantedCanary(
        id="x", type="markdown", path="README.md", marker="abc", armed_at=past
    )
    assert c.is_armed() is True


def test_is_armed_naive_armed_at_treated_as_utc() -> None:
    naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    c = PlantedCanary(
        id="x", type="markdown", path="README.md", marker="abc", armed_at=naive_past
    )
    assert c.is_armed() is True


def test_plant_with_arm_at_marks_canaries_dormant(tmp_path: Path) -> None:
    _seed_markdown(tmp_path)
    future = "2099-01-01T00:00:00Z"
    res = runner.invoke(
        app, ["plant", "--type", "markdown", "--arm-at", future, "--root", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    state = load_state(tmp_path)
    assert state.canaries
    for c in state.canaries:
        assert c.armed_at is not None
        assert c.armed_at.year == 2099


def test_plant_arm_at_invalid_errors(tmp_path: Path) -> None:
    _seed_markdown(tmp_path)
    res = runner.invoke(
        app,
        ["plant", "--type", "markdown", "--arm-at", "not-a-date", "--root", str(tmp_path)],
    )
    assert res.exit_code == 2
    assert "bad --arm-at" in res.output


def test_check_skips_dormant_canaries(tmp_path: Path) -> None:
    """A dormant canary whose sentinel has been clobbered must NOT fire."""
    _seed_markdown(tmp_path)
    future = "2099-01-01T00:00:00Z"
    runner.invoke(
        app, ["plant", "--type", "markdown", "--arm-at", future, "--root", str(tmp_path)]
    )
    # Clobber the planted file to simulate an agent rewriting it.
    (tmp_path / "README.md").write_text("# rewritten\n", encoding="utf-8")
    fires = scan(tmp_path)
    assert fires == []


def test_check_armed_canary_still_fires(tmp_path: Path) -> None:
    """Once armed (past timestamp), the same rewrite must fire."""
    _seed_markdown(tmp_path)
    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runner.invoke(
        app, ["plant", "--type", "markdown", "--arm-at", past, "--root", str(tmp_path)]
    )
    (tmp_path / "README.md").write_text("# rewritten\n", encoding="utf-8")
    fires = scan(tmp_path)
    assert fires, "armed canary should fire when sentinel is gone"


def test_arm_command_flips_dormant_to_live(tmp_path: Path) -> None:
    _seed_markdown(tmp_path)
    future = "2099-01-01T00:00:00Z"
    runner.invoke(
        app, ["plant", "--type", "markdown", "--arm-at", future, "--root", str(tmp_path)]
    )
    state = load_state(tmp_path)
    target_id = state.canaries[0].id
    res = runner.invoke(app, ["arm", target_id, "--root", str(tmp_path)])
    assert res.exit_code == 0, res.output
    after = load_state(tmp_path)
    armed = next(c for c in after.canaries if c.id == target_id)
    assert armed.is_armed() is True


def test_arm_command_unknown_id_errors(tmp_path: Path) -> None:
    _seed_markdown(tmp_path)
    runner.invoke(app, ["plant", "--type", "markdown", "--root", str(tmp_path)])
    res = runner.invoke(app, ["arm", "does-not-exist", "--root", str(tmp_path)])
    assert res.exit_code == 1
    assert "no canary matches" in res.output


def test_arm_already_armed_is_noop(tmp_path: Path) -> None:
    _seed_markdown(tmp_path)
    runner.invoke(app, ["plant", "--type", "markdown", "--root", str(tmp_path)])
    state = load_state(tmp_path)
    target_id = state.canaries[0].id
    res = runner.invoke(app, ["arm", target_id, "--root", str(tmp_path)])
    assert res.exit_code == 0
    assert "already armed" in res.output


def test_list_shows_dormant_and_armed_status(tmp_path: Path) -> None:
    _seed_markdown(tmp_path)
    # Plant one dormant.
    runner.invoke(
        app,
        [
            "plant",
            "--type",
            "markdown",
            "--arm-at",
            "2099-01-01T00:00:00Z",
            "--root",
            str(tmp_path),
        ],
    )
    # And one always-armed by hand-saving state.
    state = load_state(tmp_path)
    state.canaries.append(
        PlantedCanary(
            id="manual-1", type="todo", path="manual.py", marker="mmm"
        )
    )
    save_state(tmp_path, state)
    res = runner.invoke(app, ["list", "--root", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "dormant" in res.output
    assert "armed" in res.output


def test_config_arm_at_applied_to_plant(tmp_path: Path) -> None:
    _seed_markdown(tmp_path)
    (tmp_path / "canary.toml").write_text(
        '[canary]\ntypes = ["markdown"]\narm_at = "2099-01-01T00:00:00Z"\n',
        encoding="utf-8",
    )
    res = runner.invoke(app, ["plant", "--type", "markdown", "--root", str(tmp_path)])
    assert res.exit_code == 0, res.output
    state = load_state(tmp_path)
    assert state.canaries
    for c in state.canaries:
        assert c.armed_at is not None
        assert c.armed_at.year == 2099


def test_state_backwards_compatible_without_armed_at(tmp_path: Path) -> None:
    """Old state files without armed_at must still load."""
    (tmp_path / ".canary-cage").mkdir()
    (tmp_path / ".canary-cage" / "state.json").write_text(
        '{"schema_version": 1, "canaries": ['
        '{"id": "legacy", "type": "markdown", "path": "README.md",'
        ' "marker": "abc", "planted_at": "2025-01-01T00:00:00+00:00"}'
        "]}\n",
        encoding="utf-8",
    )
    state = load_state(tmp_path)
    assert len(state.canaries) == 1
    assert state.canaries[0].armed_at is None
    assert state.canaries[0].is_armed() is True


def test_state_roundtrip_preserves_armed_at(tmp_path: Path) -> None:
    when = datetime(2099, 1, 1, tzinfo=UTC)
    s = CageState(
        canaries=[
            PlantedCanary(
                id="x", type="markdown", path="README.md", marker="abc", armed_at=when
            )
        ]
    )
    save_state(tmp_path, s)
    loaded = load_state(tmp_path)
    assert loaded.canaries[0].armed_at == when
