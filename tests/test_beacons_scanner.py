"""Tests for beacons + scanner (M4)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from canary_cage.beacons import BeaconRecord, FileBeacon, LogBeacon
from canary_cage.beacons.file import fired_dir
from canary_cage.beacons.log import log_path
from canary_cage.canaries import MarkdownCanary, TodoCanary
from canary_cage.cli import app
from canary_cage.scanner import scan
from canary_cage.state import CageState, PlantedCanary, save_state

runner = CliRunner()


# ---------- beacon adapters ----------

def test_file_beacon_writes_per_id(tmp_path: Path) -> None:
    rec = BeaconRecord(
        canary_id="md-deadbeef",
        canary_type="markdown",
        source="working-tree",
        detail="sentinel missing",
        path="README.md",
    )
    FileBeacon().fire(tmp_path, rec)
    target = fired_dir(tmp_path) / "md-deadbeef.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["canary_id"] == "md-deadbeef"
    assert payload["source"] == "working-tree"


def test_log_beacon_appends_jsonl(tmp_path: Path) -> None:
    beacon = LogBeacon()
    for i in range(2):
        beacon.fire(
            tmp_path,
            BeaconRecord(
                canary_id=f"id-{i}",
                canary_type="markdown",
                source="working-tree",
                detail="x",
            ),
        )
    lines = log_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["canary_id"] == "id-0"


# ---------- scanner: working-tree drift ----------

def test_scan_detects_sentinel_removal(tmp_path: Path) -> None:
    # Plant a markdown canary in a real README, then nuke the file.
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    planted = MarkdownCanary().plant(tmp_path)
    assert planted, "expected at least one planted canary"
    save_state(tmp_path, CageState(canaries=planted))

    # Simulate an agent rewriting the README and dropping the canary.
    (tmp_path / "README.md").write_text("# hi (rewritten)\n", encoding="utf-8")

    fires = scan(tmp_path)
    assert len(fires) == 1
    assert fires[0].source == "working-tree"
    assert fires[0].canary_id == planted[0].id
    # And the file beacon recorded it.
    assert (fired_dir(tmp_path) / f"{planted[0].id}.json").exists()


def test_scan_silent_when_intact(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("notes\n", encoding="utf-8")
    planted = MarkdownCanary().plant(tmp_path)
    save_state(tmp_path, CageState(canaries=planted))
    assert scan(tmp_path) == []


def test_scan_detects_vanished_file(tmp_path: Path) -> None:
    (tmp_path / "x.sh").write_text("echo hi\n", encoding="utf-8")
    planted = TodoCanary().plant(tmp_path)
    save_state(tmp_path, CageState(canaries=planted))
    (tmp_path / "x.sh").unlink()
    fires = scan(tmp_path)
    assert len(fires) == 1
    assert "vanished" in fires[0].detail


# ---------- scanner: stray-file signal ----------

def test_scan_detects_agent_dropped_artifact(tmp_path: Path) -> None:
    canary = PlantedCanary(
        id="py-cafebabe",
        type="docstring",
        path="src/mod.py",
        marker="cafebabe",
    )
    save_state(tmp_path, CageState(canaries=[canary]))
    # Even with no planted file present, an "agent dropped" file in
    # fired/<marker> should surface as a fire.
    target_dir = fired_dir(tmp_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "cafebabe").write_text("oops\n", encoding="utf-8")
    # Also create the planted file so working-tree check stays quiet for it.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        '"""m.\n\ncanary-cage:py:BEGIN:cafebabe:pad1\nx\ncanary-cage:py:END:cafebabe\n"""\n',
        encoding="utf-8",
    )
    fires = scan(tmp_path)
    sources = [f.source for f in fires]
    assert "stray-file" in sources


# ---------- scanner: git-history signal ----------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_scan_detects_marker_in_git_history(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "init")

    planted = MarkdownCanary().plant(tmp_path)
    save_state(tmp_path, CageState(canaries=planted))
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "plant canary")

    fires = scan(tmp_path)
    sources = {f.source for f in fires}
    assert "git-history" in sources


# ---------- CLI integration ----------

def test_cli_check_clean(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    plant = runner.invoke(app, ["plant", "--type", "markdown", "--root", str(tmp_path)])
    assert plant.exit_code == 0, plant.stdout
    check = runner.invoke(app, ["check", "--root", str(tmp_path)])
    assert check.exit_code == 0
    assert "singing" in check.stdout


def test_cli_check_fires_when_canary_removed(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    runner.invoke(app, ["plant", "--type", "markdown", "--root", str(tmp_path)])
    (tmp_path / "README.md").write_text("# wiped\n", encoding="utf-8")
    check = runner.invoke(app, ["check", "--root", str(tmp_path)])
    assert check.exit_code == 1
    assert "fire" in check.stdout.lower()
