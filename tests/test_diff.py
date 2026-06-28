"""Tests for `canary diff` — verdicts, --json, --since, table output."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from canary_cage.cli import app
from canary_cage.diff import (
    VERDICT_FIRED,
    VERDICT_INTACT,
    VERDICT_MUTATED,
    VERDICT_REMOVED,
    compute_diff,
)
from canary_cage.state import load_state

runner = CliRunner()


def _plant(root: Path) -> None:
    # Seed a markdown file so MarkdownCanary has something to plant in.
    (root / "README.md").write_text("# hi\n", encoding="utf-8")
    result = runner.invoke(app, ["plant", "--type", "markdown", "--root", str(root)])
    assert result.exit_code == 0, result.stdout


def test_diff_clean_repo_no_changes(tmp_path: Path) -> None:
    _plant(tmp_path)
    report = compute_diff(tmp_path)
    assert report.total_planted >= 1
    assert all(d.verdict == VERDICT_INTACT for d in report.diffs)

    result = runner.invoke(app, ["diff", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "no diff" in result.stdout or "unchanged" in result.stdout


def test_diff_mutated_canary(tmp_path: Path) -> None:
    _plant(tmp_path)
    state = load_state(tmp_path)
    target = tmp_path / state.canaries[0].path
    text = target.read_text(encoding="utf-8")
    # Wipe the END sentinel — payload tampered, BEGIN still present.
    end_token = f"canary-cage:md:END:{state.canaries[0].marker}"
    assert end_token in text
    target.write_text(text.replace(end_token, "REMOVED"), encoding="utf-8")

    report = compute_diff(tmp_path)
    verdicts = {d.canary_id: d.verdict for d in report.diffs}
    assert verdicts[state.canaries[0].id] == VERDICT_MUTATED

    result = runner.invoke(app, ["diff", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "mutated" in result.stdout


def test_diff_removed_canary(tmp_path: Path) -> None:
    _plant(tmp_path)
    state = load_state(tmp_path)
    target = tmp_path / state.canaries[0].path
    target.unlink()

    report = compute_diff(tmp_path)
    assert any(d.verdict == VERDICT_REMOVED for d in report.diffs)

    result = runner.invoke(app, ["diff", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "removed" in result.stdout


def test_diff_fired_canary(tmp_path: Path) -> None:
    _plant(tmp_path)
    state = load_state(tmp_path)
    canary = state.canaries[0]
    # Drop a stray fire artifact named after the marker — scanner will
    # flag this as a stray-file fire, which upgrades verdict to "fired".
    fired_dir = tmp_path / ".canary-cage" / "fired"
    fired_dir.mkdir(parents=True, exist_ok=True)
    (fired_dir / canary.marker).write_text("{}", encoding="utf-8")

    report = compute_diff(tmp_path)
    target = next(d for d in report.diffs if d.canary_id == canary.id)
    assert target.verdict == VERDICT_FIRED
    assert "stray-file" in target.fire_sources


def test_diff_json_schema(tmp_path: Path) -> None:
    _plant(tmp_path)
    result = runner.invoke(app, ["diff", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert "groups" in payload
    assert payload["total_planted"] >= 1
    assert payload["since"] is None
    # Each group has a canary_type and items list with stable keys.
    for group in payload["groups"]:
        assert "canary_type" in group
        for item in group["items"]:
            for key in ("canary_id", "canary_type", "path", "verdict", "detail", "fire_sources"):
                assert key in item


def test_diff_since_filters_to_changed_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "OTHER.md").write_text("# other\n", encoding="utf-8")
    result = runner.invoke(app, ["plant", "--type", "markdown", "--root", str(tmp_path)])
    assert result.exit_code == 0

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "plant"], cwd=tmp_path, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Tamper one file only, commit it so it shows up in `git diff base...HEAD`.
    state = load_state(tmp_path)
    readme_canary = next(c for c in state.canaries if c.path == "README.md")
    readme_path = tmp_path / "README.md"
    text = readme_path.read_text(encoding="utf-8")
    readme_path.write_text(
        text.replace(f"canary-cage:md:END:{readme_canary.marker}", "GONE"),
        encoding="utf-8",
    )
    subprocess.run(["git", "commit", "-q", "-am", "tamper"], cwd=tmp_path, check=True)

    report = compute_diff(tmp_path, since=base_sha)
    paths = {d.path for d in report.diffs}
    assert paths == {"README.md"}, f"unexpected paths: {paths}"
    assert report.diffs[0].verdict == VERDICT_MUTATED
