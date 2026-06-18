"""Per-type tests for the M3 canary types."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from canary_cage.canaries.docstring import MARKER_PREFIX as DOC_PREFIX
from canary_cage.canaries.docstring import DocstringCanary
from canary_cage.canaries.todo import MARKER_PREFIX as TODO_PREFIX
from canary_cage.canaries.todo import TodoCanary
from canary_cage.cli import app
from canary_cage.state import load_state

runner = CliRunner()


# ---------------------------------------------------------------------------
# Docstring canary
# ---------------------------------------------------------------------------


def test_docstring_plant_uproot_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "pkg" / "mod.py"
    src.parent.mkdir()
    original = '"""Module docstring.\n\nSecond paragraph.\n"""\n\nVALUE = 1\n'
    src.write_text(original, encoding="utf-8")

    canary = DocstringCanary()
    planted = canary.plant(tmp_path)
    assert len(planted) == 1
    new_content = src.read_text(encoding="utf-8")
    assert DOC_PREFIX in new_content
    # Module must still parse as Python after planting.
    compile(new_content, str(src), "exec")
    # And VALUE = 1 should still be intact below the docstring.
    assert "VALUE = 1" in new_content

    canary.uproot(tmp_path, planted[0])
    assert src.read_text(encoding="utf-8") == original


def test_docstring_skips_files_without_docstring(tmp_path: Path) -> None:
    src = tmp_path / "no_doc.py"
    src.write_text("x = 1\n", encoding="utf-8")
    planted = DocstringCanary().plant(tmp_path)
    assert planted == []
    assert src.read_text(encoding="utf-8") == "x = 1\n"


def test_docstring_is_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text('"""hi."""\n', encoding="utf-8")
    first = DocstringCanary().plant(tmp_path)
    second = DocstringCanary().plant(tmp_path)
    assert len(first) == 1
    assert second == []


def test_docstring_ignores_dot_dirs(tmp_path: Path) -> None:
    hidden = tmp_path / ".venv" / "x.py"
    hidden.parent.mkdir()
    hidden.write_text('"""ignored."""\n', encoding="utf-8")
    planted = DocstringCanary().plant(tmp_path)
    assert planted == []


# ---------------------------------------------------------------------------
# Fake-TODO canary
# ---------------------------------------------------------------------------


def test_todo_plant_uproot_round_trip(tmp_path: Path) -> None:
    files = {
        "run.sh": "#!/bin/sh\necho hi\n",
        "conf.toml": "[a]\nb = 1\n",
        "pkg/x.py": "x = 1\n",
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    canary = TodoCanary()
    planted = canary.plant(tmp_path)
    assert len(planted) == 3
    for rel in files:
        assert TODO_PREFIX in (tmp_path / rel).read_text(encoding="utf-8")

    for p in planted:
        canary.uproot(tmp_path, p)
    for rel, body in files.items():
        assert (tmp_path / rel).read_text(encoding="utf-8") == body


def test_todo_skips_unknown_extensions(tmp_path: Path) -> None:
    (tmp_path / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (tmp_path / "doc.md").write_text("# hi\n", encoding="utf-8")
    planted = TodoCanary().plant(tmp_path)
    assert planted == []


# ---------------------------------------------------------------------------
# CLI integration: plant --type all + list
# ---------------------------------------------------------------------------


def _seed_mixed_repo(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Hello\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""pkg root."""\n', encoding="utf-8")
    (tmp_path / "script.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")


def test_plant_all_then_list(tmp_path: Path) -> None:
    _seed_mixed_repo(tmp_path)

    plant_result = runner.invoke(app, ["plant", "--type", "all", "--root", str(tmp_path)])
    assert plant_result.exit_code == 0, plant_result.stdout

    state = load_state(tmp_path)
    types = {c.type for c in state.canaries}
    assert types == {"markdown", "docstring", "todo"}

    list_result = runner.invoke(app, ["list", "--root", str(tmp_path)])
    assert list_result.exit_code == 0
    for t in ("markdown", "docstring", "todo"):
        assert t in list_result.stdout


def test_plant_all_then_uproot_clean(tmp_path: Path) -> None:
    _seed_mixed_repo(tmp_path)
    originals = {p: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file()}

    runner.invoke(app, ["plant", "--type", "all", "--root", str(tmp_path)])
    runner.invoke(app, ["uproot", "--root", str(tmp_path)])

    for p, body in originals.items():
        assert p.read_text(encoding="utf-8") == body, f"{p} not restored"
