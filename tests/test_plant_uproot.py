"""Round-trip tests for plant + uproot."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from canary_cage.canaries.markdown import MARKER_PREFIX
from canary_cage.cli import app
from canary_cage.state import load_state, state_path

runner = CliRunner()


def _seed_repo(tmp_path: Path) -> tuple[Path, str, str]:
    readme = tmp_path / "README.md"
    readme_original = "# Hello\n\nSome content.\n"
    readme.write_text(readme_original, encoding="utf-8")

    nested = tmp_path / "docs" / "guide.md"
    nested.parent.mkdir(parents=True)
    nested_original = "# Guide\n"
    nested.write_text(nested_original, encoding="utf-8")

    # A dotfile dir that should be ignored.
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("nope\n", encoding="utf-8")
    return tmp_path, readme_original, nested_original


def test_plant_then_uproot_round_trip(tmp_path: Path) -> None:
    repo, readme_original, nested_original = _seed_repo(tmp_path)

    plant_result = runner.invoke(app, ["plant", "--type", "markdown", "--root", str(repo)])
    assert plant_result.exit_code == 0, plant_result.stdout
    assert "planted 2" in plant_result.stdout

    # Both files should now carry the marker; the .github file should not.
    assert MARKER_PREFIX in (repo / "README.md").read_text(encoding="utf-8")
    assert MARKER_PREFIX in (repo / "docs/guide.md").read_text(encoding="utf-8")
    assert MARKER_PREFIX not in (repo / ".github/PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )

    state = load_state(repo)
    assert len(state.canaries) == 2
    assert state_path(repo).exists()

    uproot_result = runner.invoke(app, ["uproot", "--root", str(repo)])
    assert uproot_result.exit_code == 0, uproot_result.stdout
    assert "uprooted 2" in uproot_result.stdout

    # Files should be byte-for-byte identical to their pre-plant content.
    assert (repo / "README.md").read_text(encoding="utf-8") == readme_original
    assert (repo / "docs/guide.md").read_text(encoding="utf-8") == nested_original

    state = load_state(repo)
    assert state.canaries == []


def test_plant_unknown_type(tmp_path: Path) -> None:
    result = runner.invoke(app, ["plant", "--type", "bogus", "--root", str(tmp_path)])
    assert result.exit_code == 2
    assert "unknown canary type" in result.stdout


def test_uproot_with_nothing_planted(tmp_path: Path) -> None:
    result = runner.invoke(app, ["uproot", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "nothing to uproot" in result.stdout


def test_plant_is_idempotent_per_file(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "README.md").write_text("# Hi\n", encoding="utf-8")

    first = runner.invoke(app, ["plant", "--type", "markdown", "--root", str(repo)])
    assert first.exit_code == 0
    second = runner.invoke(app, ["plant", "--type", "markdown", "--root", str(repo)])
    assert second.exit_code == 0
    # Second run should find no eligible files (already planted).
    assert "no eligible files" in second.stdout

    state = load_state(repo)
    assert len(state.canaries) == 1
