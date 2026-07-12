"""Tests for ``scripts/action_runner.py`` — the GitHub Action entrypoint.

The runner shells out to the ``canary`` CLI, so we don't need to plant
canaries or exercise the scanner here. We stub the CLI with a tiny
shell script that echoes fixture JSON and asserts on the runner's
outputs: exit code, GitHub Actions output file, step summary, PR
comment body, and the on-disk report artifact.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "scripts" / "action_runner.py"


@pytest.fixture(scope="module")
def runner_module():
    spec = importlib.util.spec_from_file_location("action_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["action_runner"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _stub_canary_bin(tmp_path: Path, stdout: str, exit_code: int) -> Path:
    """Write a fake ``canary`` executable that prints ``stdout`` and exits."""
    bin_path = tmp_path / "canary-stub.sh"
    # Escape single quotes for embedding in the heredoc body.
    escaped = stdout.replace("'", "'\\''")
    bin_path.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'CANARY_STUB_EOF'\n{escaped}\nCANARY_STUB_EOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


def _base_env(tmp_path: Path, canary_bin: Path, **overrides: str) -> dict[str, str]:
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "canary.toml").write_text("[canary]\ntypes = [\"markdown\"]\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CANARY_CAGE_BIN": str(canary_bin),
        "CANARY_CAGE_WORKDIR": str(workdir),
        "CANARY_CAGE_CONFIG": "canary.toml",
        "CANARY_CAGE_FAIL_ON_FIRE": "true",
        "GITHUB_WORKSPACE": str(workdir),
        "GITHUB_OUTPUT": str(tmp_path / "outputs.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
    }
    env.update(overrides)
    return env


def _parse_outputs(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file into a dict (supports key=val + heredoc)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and "=" not in line.split("<<", 1)[0]:
            key, delim = line.split("<<", 1)
            i += 1
            buf: list[str] = []
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[key.strip()] = "\n".join(buf)
            i += 1
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            out[key.strip()] = val
        i += 1
    return out


def test_runner_clean_exits_zero(tmp_path, runner_module):
    canary_bin = _stub_canary_bin(
        tmp_path,
        json.dumps({"schema_version": 1, "root": "/repo", "fires": []}),
        exit_code=0,
    )
    env = _base_env(tmp_path, canary_bin)

    rc = runner_module.run([], env=env)

    assert rc == 0
    outputs = _parse_outputs(Path(env["GITHUB_OUTPUT"]))
    assert outputs["fired-count"] == "0"
    assert outputs["fired-ids"] == "[]"
    assert Path(outputs["report-path"]).exists()

    summary = Path(env["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "all quiet" in summary
    assert "<!-- canary-cage:pr-comment -->" in summary


def test_runner_reports_fires_and_fails_when_configured(tmp_path, runner_module):
    payload = {
        "schema_version": 1,
        "root": "/repo",
        "fires": [
            {
                "canary_id": "md-abc123",
                "canary_type": "markdown",
                "source": "working-tree",
                "detail": "sentinel missing from docs/notes.md",
                "path": "docs/notes.md",
                "attributed_to": {
                    "top": {"agent": "test-agent", "confidence": 0.87},
                },
            },
            {
                "canary_id": "todo-xyz789",
                "canary_type": "todo",
                "source": "git-log",
                "detail": "TODO removed in commit deadbeef",
                "path": "src/hello.py",
                "attributed_to": None,
            },
        ],
    }
    canary_bin = _stub_canary_bin(tmp_path, json.dumps(payload), exit_code=1)
    env = _base_env(tmp_path, canary_bin)

    rc = runner_module.run([], env=env)

    assert rc == 1
    outputs = _parse_outputs(Path(env["GITHUB_OUTPUT"]))
    assert outputs["fired-count"] == "2"
    assert json.loads(outputs["fired-ids"]) == ["md-abc123", "todo-xyz789"]

    report = json.loads(Path(outputs["report-path"]).read_text(encoding="utf-8"))
    assert len(report["fires"]) == 2

    comment = Path(outputs["comment-path"]).read_text(encoding="utf-8")
    assert "2 canary fire(s) detected" in comment
    assert "md-abc123" in comment
    assert "todo-xyz789" in comment
    assert "test-agent" in comment
    assert "0.87" in comment

    summary = Path(env["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "2 canary fire(s) detected" in summary


def test_runner_respects_fail_on_fire_false(tmp_path, runner_module):
    payload = {
        "schema_version": 1,
        "root": "/repo",
        "fires": [
            {
                "canary_id": "md-abc123",
                "canary_type": "markdown",
                "source": "working-tree",
                "detail": "sentinel missing",
                "path": "docs/notes.md",
            }
        ],
    }
    canary_bin = _stub_canary_bin(tmp_path, json.dumps(payload), exit_code=1)
    env = _base_env(tmp_path, canary_bin, CANARY_CAGE_FAIL_ON_FIRE="false")

    rc = runner_module.run([], env=env)

    assert rc == 0
    outputs = _parse_outputs(Path(env["GITHUB_OUTPUT"]))
    assert outputs["fired-count"] == "1"


def test_runner_returns_2_on_canary_hard_error(tmp_path, runner_module):
    canary_bin = _stub_canary_bin(tmp_path, "bad-config: not valid json", exit_code=2)
    env = _base_env(tmp_path, canary_bin)

    rc = runner_module.run([], env=env)

    # Unparseable stdout counts as a hard error (rc 2), regardless of exit.
    assert rc == 2


def test_runner_returns_2_when_canary_missing(tmp_path, runner_module):
    missing_bin = tmp_path / "does-not-exist"
    env = _base_env(tmp_path, missing_bin)

    rc = runner_module.run([], env=env)

    assert rc == 2


def test_build_markdown_no_fires_uses_all_quiet_headline(runner_module):
    md = runner_module.build_markdown({"fires": []})
    assert "<!-- canary-cage:pr-comment -->" in md
    assert "all quiet" in md


def test_build_markdown_escapes_pipes_in_detail(runner_module):
    md = runner_module.build_markdown(
        {
            "fires": [
                {
                    "canary_id": "md-x",
                    "canary_type": "markdown",
                    "source": "working-tree",
                    "detail": "pipe|inside|detail",
                    "path": "docs/notes.md",
                }
            ]
        }
    )
    # Table pipes stay valid: injected pipes are escaped so the row still
    # has exactly the columns we advertised in the header.
    row = next(line for line in md.splitlines() if "pipe" in line)
    # Count only unescaped '|' — every '|' preceded by a '\' is escaped.
    unescaped = sum(
        1
        for i, ch in enumerate(row)
        if ch == "|" and (i == 0 or row[i - 1] != "\\")
    )
    # Header is `| a | b | c | d | e |` \u2192 6 pipes. Same for a well-formed row.
    assert unescaped == 6


def _parse_action_yaml(path: Path) -> dict:
    """Best-effort YAML parse without a hard PyYAML dep."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - happy path uses PyYAML
        pytest.skip("PyYAML not installed; skipping action.yml shape test")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_action_yaml_declares_expected_shape():
    action_path = REPO_ROOT / "action.yml"
    assert action_path.is_file(), "action.yml must live at repo root for GitHub to find it"
    doc = _parse_action_yaml(action_path)

    assert doc.get("name"), "action.yml missing top-level name"
    assert doc.get("description"), "action.yml missing description"
    runs = doc.get("runs") or {}
    assert runs.get("using") == "composite", "expected a composite action"

    inputs = doc.get("inputs") or {}
    for required_input in (
        "config-path",
        "fail-on-fire",
        "since",
        "comment-on-pr",
    ):
        assert required_input in inputs, f"action.yml missing input: {required_input}"

    outputs = doc.get("outputs") or {}
    for required_output in ("fired-count", "fired-ids", "report-path"):
        assert required_output in outputs, f"action.yml missing output: {required_output}"

    steps = runs.get("steps") or []
    step_names = [s.get("name", "") for s in steps]
    assert any("canary" in n.lower() for n in step_names), (
        f"expected a canary check step; got {step_names!r}"
    )


def test_pyproject_still_installs_expected_console_script():
    # The action's install step depends on the `canary` entrypoint being
    # registered. A rename would silently break every downstream user.
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject.get("project", {}).get("scripts", {})
    assert scripts.get("canary") == "canary_cage.cli:app", (
        f"pyproject.toml [project.scripts] canary entry changed: {scripts!r}"
    )


def test_action_runner_uses_canary_cli_json_flag():
    # Guardrail: if someone drops the `--json` flag from the runner, the
    # entire pipeline (report + summary + comment) collapses.
    src = (REPO_ROOT / "scripts" / "action_runner.py").read_text(encoding="utf-8")
    assert '"--json"' in src, "action_runner.py must invoke `canary check --json`"
    assert '"check"' in src, "action_runner.py must invoke `canary check`"
