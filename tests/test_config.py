"""Tests for ``canary_cage.config`` and ``canary init`` CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from canary_cage.cli import app
from canary_cage.config import (
    CONFIG_FILE_NAME,
    CageConfig,
    PlantFilter,
    load_config,
    write_default_config,
)

runner = CliRunner()


def test_defaults_when_no_config(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.types == ["markdown", "docstring", "todo", "manifest"]
    assert cfg.density == 1.0
    assert cfg.ignore == []
    assert cfg.preset is None


def test_explicit_fields_override_preset(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\npreset = "minimal"\ntypes = ["todo"]\ndensity = 0.5\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.preset == "minimal"
    assert cfg.types == ["todo"]  # explicit wins over preset's ["markdown"]
    assert cfg.density == 0.5


def test_preset_alone_seeds_defaults(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\npreset = "chaotic-good"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.preset == "chaotic-good"
    assert set(cfg.types) == {"markdown", "docstring", "todo", "manifest"}
    assert cfg.density == 0.5


def test_unknown_preset_raises(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\npreset = "nope"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_density_out_of_range_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        CageConfig(density=1.5)


def test_plant_filter_respects_ignore(tmp_path: Path) -> None:
    (tmp_path / "keep.md").write_text("k", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "drop.md").write_text("d", encoding="utf-8")
    cfg = CageConfig(ignore=["docs/**"])
    pf = PlantFilter(cfg)
    selected = pf.select(
        tmp_path, [tmp_path / "keep.md", tmp_path / "docs" / "drop.md"]
    )
    assert selected == [tmp_path / "keep.md"]


def test_plant_filter_default_ignores_cage_dir(tmp_path: Path) -> None:
    (tmp_path / ".canary-cage").mkdir()
    (tmp_path / ".canary-cage" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / "real.md").write_text("y", encoding="utf-8")
    pf = PlantFilter(CageConfig())
    selected = pf.select(
        tmp_path,
        [tmp_path / ".canary-cage" / "x.md", tmp_path / "real.md"],
    )
    assert selected == [tmp_path / "real.md"]


def test_density_halving_is_deterministic(tmp_path: Path) -> None:
    files = [tmp_path / f"f{i}.md" for i in range(10)]
    for f in files:
        f.write_text("x", encoding="utf-8")
    pf = PlantFilter(CageConfig(density=0.5))
    a = pf.select(tmp_path, files)
    b = pf.select(tmp_path, files)
    assert a == b
    assert len(a) == 5


def test_density_zero_skips_everything(tmp_path: Path) -> None:
    files = [tmp_path / f"f{i}.md" for i in range(3)]
    for f in files:
        f.write_text("x", encoding="utf-8")
    pf = PlantFilter(CageConfig(density=0.0))
    assert pf.select(tmp_path, files) == []


def test_write_default_config(tmp_path: Path) -> None:
    path = write_default_config(tmp_path)
    assert path.exists()
    assert "[canary]" in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_default_config(tmp_path)
    write_default_config(tmp_path, overwrite=True)  # no raise


def test_cli_init_writes_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    text = (tmp_path / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    assert "[canary]" in text


def test_cli_init_with_preset_activates_line(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["init", "--root", str(tmp_path), "--preset", "paranoid"]
    )
    assert result.exit_code == 0, result.output
    text = (tmp_path / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    assert 'preset = "paranoid"' in text
    # And it should load cleanly.
    cfg = load_config(tmp_path)
    assert cfg.preset == "paranoid"


def test_cli_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("# existing\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert (tmp_path / CONFIG_FILE_NAME).read_text(encoding="utf-8") == "# existing\n"


def test_cli_init_force_overwrites(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("# existing\n", encoding="utf-8")
    result = runner.invoke(
        app, ["init", "--root", str(tmp_path), "--force"]
    )
    assert result.exit_code == 0
    assert "[canary]" in (tmp_path / CONFIG_FILE_NAME).read_text(encoding="utf-8")


def test_plant_honours_config_ignore(tmp_path: Path) -> None:
    (tmp_path / "keep.md").write_text("k\n", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "drop.md").write_text("d\n", encoding="utf-8")
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\ntypes = ["markdown"]\nignore = ["vendor/**"]\n',
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["plant", "--type", "markdown", "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "canary-cage:md" in (tmp_path / "keep.md").read_text(encoding="utf-8")
    assert "canary-cage:md" not in (tmp_path / "vendor" / "drop.md").read_text(
        encoding="utf-8"
    )


def test_plant_honours_config_types(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("k\n", encoding="utf-8")
    (tmp_path / "x.py").write_text('"""mod."""\n', encoding="utf-8")
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\ntypes = ["markdown"]\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["plant", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "canary-cage:md" in (tmp_path / "x.md").read_text(encoding="utf-8")
    # docstring canary was filtered out by config.types
    assert "canary-cage:py" not in (tmp_path / "x.py").read_text(encoding="utf-8")
