"""Tests for the dependency-manifest canary (issue #21)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from canary_cage.canaries.manifest import (
    MARKER_PREFIX,
    PACKAGE_PREFIX,
    ManifestCanary,
)
from canary_cage.cli import app
from canary_cage.scanner import scan
from canary_cage.state import load_state

runner = CliRunner()


def test_requirements_plant_uproot_round_trip(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    original = "requests>=2.0\nrich\n"
    req.write_text(original, encoding="utf-8")

    canary = ManifestCanary()
    planted = canary.plant(tmp_path)
    assert len(planted) == 1
    text = req.read_text(encoding="utf-8")
    assert MARKER_PREFIX in text
    assert PACKAGE_PREFIX in text
    # Original lines must be preserved verbatim.
    assert text.startswith(original)

    canary.uproot(tmp_path, planted[0])
    assert req.read_text(encoding="utf-8") == original


def test_pyproject_plant_uproot_round_trip(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    original = (
        '[project]\n'
        'name = "demo"\n'
        'version = "0.1.0"\n'
        'dependencies = [\n'
        '    "rich>=13",\n'
        '    "typer>=0.12",\n'
        ']\n'
    )
    py.write_text(original, encoding="utf-8")

    canary = ManifestCanary()
    planted = canary.plant(tmp_path)
    assert len(planted) == 1
    text = py.read_text(encoding="utf-8")
    assert MARKER_PREFIX in text
    # File must still be valid TOML.
    import tomllib

    parsed = tomllib.loads(text)
    deps = parsed["project"]["dependencies"]
    assert any(PACKAGE_PREFIX in d for d in deps)
    assert "rich>=13" in deps and "typer>=0.12" in deps

    canary.uproot(tmp_path, planted[0])
    assert py.read_text(encoding="utf-8") == original


def test_pyproject_inline_array(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\ndependencies = ["rich", "typer"]\n'
    py.write_text(original, encoding="utf-8")

    canary = ManifestCanary()
    planted = canary.plant(tmp_path)
    assert len(planted) == 1
    import tomllib

    parsed = tomllib.loads(py.read_text(encoding="utf-8"))
    deps = parsed["project"]["dependencies"]
    assert "rich" in deps and "typer" in deps
    assert any(PACKAGE_PREFIX in d for d in deps)

    canary.uproot(tmp_path, planted[0])
    assert py.read_text(encoding="utf-8") == original


def test_pyproject_without_dependencies_skipped(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    planted = ManifestCanary().plant(tmp_path)
    assert planted == []


def test_plant_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("rich\n", encoding="utf-8")
    canary = ManifestCanary()
    first = canary.plant(tmp_path)
    second = canary.plant(tmp_path)
    assert len(first) == 1
    assert second == []


def test_ignores_dot_dirs(tmp_path: Path) -> None:
    hidden = tmp_path / ".venv" / "requirements.txt"
    hidden.parent.mkdir()
    hidden.write_text("rich\n", encoding="utf-8")
    planted = ManifestCanary().plant(tmp_path)
    assert planted == []


def test_scanner_detects_mutation(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("rich\n", encoding="utf-8")
    planted = ManifestCanary().plant(tmp_path)
    assert planted
    # Persist state so scan() can find the canary.
    from canary_cage.state import CageState, save_state

    save_state(tmp_path, CageState(canaries=planted))
    # Sanity: clean scan finds nothing (ignoring git/lockfile checks).
    fires = scan(tmp_path, beacons=[])
    assert fires == []

    # Agent "cleaned up" the trap line.
    (tmp_path / "requirements.txt").write_text("rich\n", encoding="utf-8")
    fires = scan(tmp_path, beacons=[])
    assert any(r.source == "working-tree" for r in fires)


def test_scanner_detects_lockfile_mention(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("rich\n", encoding="utf-8")
    planted = ManifestCanary().plant(tmp_path)
    assert planted
    from canary_cage.state import CageState, save_state

    save_state(tmp_path, CageState(canaries=planted))
    marker = planted[0].marker
    # Simulate an agent resolving the trap into a lockfile.
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "canary-trip-{marker}"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    fires = scan(tmp_path, beacons=[])
    assert any(r.source == "lockfile" for r in fires)


def test_cli_plant_all_includes_manifest(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("rich\n", encoding="utf-8")
    result = runner.invoke(app, ["plant", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    state = load_state(tmp_path)
    assert any(c.type == "manifest" for c in state.canaries)


def test_cli_plant_type_manifest(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("rich\n", encoding="utf-8")
    result = runner.invoke(
        app, ["plant", "--type", "manifest", "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    state = load_state(tmp_path)
    assert len(state.canaries) == 1
    assert state.canaries[0].type == "manifest"


@pytest.mark.parametrize("variant", ["requirements", "pyproject"])
def test_cli_uproot_restores(tmp_path: Path, variant: str) -> None:
    if variant == "requirements":
        target = tmp_path / "requirements.txt"
        original = "rich\n"
    else:
        target = tmp_path / "pyproject.toml"
        original = (
            '[project]\nname = "demo"\ndependencies = [\n    "rich",\n]\n'
        )
    target.write_text(original, encoding="utf-8")
    r1 = runner.invoke(
        app, ["plant", "--type", "manifest", "--root", str(tmp_path)]
    )
    assert r1.exit_code == 0, r1.output
    assert target.read_text(encoding="utf-8") != original
    r2 = runner.invoke(app, ["uproot", "--root", str(tmp_path)])
    assert r2.exit_code == 0, r2.output
    assert target.read_text(encoding="utf-8") == original
