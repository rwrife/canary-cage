"""Tests for the pre-commit guardrail."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from canary_cage.canaries import MarkdownCanary
from canary_cage.cli import app
from canary_cage.precommit import (
    HOOK_SCRIPT,
    check_staged,
    install_hook,
)
from canary_cage.state import CageState, save_state

runner = CliRunner()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("# hello\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_clean_repo_no_violations(repo: Path) -> None:
    (repo / "other.md").write_text("# other\n", encoding="utf-8")
    _git(repo, "add", "other.md")
    assert check_staged(repo) == []


def test_removing_canary_blocks_commit(repo: Path) -> None:
    planted = MarkdownCanary().plant(repo)
    assert planted, "expected at least one canary planted"
    save_state(repo, CageState(canaries=planted))
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "plant")

    # Operator hand-edits the canary out of README.md and stages it.
    (repo / "README.md").write_text("# hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    violations = check_staged(repo)
    assert len(violations) == 1
    assert violations[0].kind == "canary-removed"
    assert violations[0].path == "README.md"


def test_deleting_canary_file_blocks_commit(repo: Path) -> None:
    planted = MarkdownCanary().plant(repo)
    save_state(repo, CageState(canaries=planted))
    # Commit the planted state first so we can stage a deletion.
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "plant")

    (repo / "README.md").unlink()
    _git(repo, "add", "-A", "README.md")

    violations = check_staged(repo)
    assert any(v.kind == "canary-deleted" for v in violations)


def test_uproot_then_stage_is_allowed(repo: Path) -> None:
    canary = MarkdownCanary()
    planted = canary.plant(repo)
    save_state(repo, CageState(canaries=planted))
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "plant")

    # Proper flow: uproot clears the payload *and* state.
    for c in planted:
        canary.uproot(repo, c)
    save_state(repo, CageState())
    _git(repo, "add", "README.md")

    assert check_staged(repo) == []


def test_staging_fired_artifact_blocks_commit(repo: Path) -> None:
    fired_dir = repo / ".canary-cage" / "fired"
    fired_dir.mkdir(parents=True)
    artifact = fired_dir / "abc123.json"
    artifact.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-f", str(artifact.relative_to(repo)))

    violations = check_staged(repo)
    assert len(violations) == 1
    assert violations[0].kind == "fired-leak"
    assert violations[0].path.endswith("abc123.json")


def test_staging_beacon_log_blocks_commit(repo: Path) -> None:
    cage = repo / ".canary-cage"
    cage.mkdir()
    log = cage / "beacon.log"
    log.write_text("fire!\n", encoding="utf-8")
    _git(repo, "add", "-f", str(log.relative_to(repo)))

    violations = check_staged(repo)
    assert [v.kind for v in violations] == ["fired-leak"]


def test_non_git_root_returns_empty(tmp_path: Path) -> None:
    assert check_staged(tmp_path) == []


def test_install_hook_writes_executable_script(repo: Path) -> None:
    path = install_hook(repo)
    assert path == repo / ".git" / "hooks" / "pre-commit"
    assert path.read_text(encoding="utf-8") == HOOK_SCRIPT
    assert path.stat().st_mode & 0o111  # executable bit set


def test_install_hook_refuses_overwrite_without_force(repo: Path) -> None:
    install_hook(repo)
    with pytest.raises(FileExistsError):
        install_hook(repo)
    install_hook(repo, force=True)  # force=True succeeds


def test_install_hook_rejects_non_git_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        install_hook(tmp_path)


def test_cli_precommit_clean_repo(repo: Path) -> None:
    result = runner.invoke(app, ["precommit", "--root", str(repo)])
    assert result.exit_code == 0
    assert "clean" in result.stdout


def test_cli_precommit_blocks_canary_removal(repo: Path) -> None:
    planted = MarkdownCanary().plant(repo)
    save_state(repo, CageState(canaries=planted))
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "plant")
    (repo / "README.md").write_text("# hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    result = runner.invoke(app, ["precommit", "--root", str(repo)])
    assert result.exit_code == 1
    assert "canary-removed" in result.stdout


def test_cli_install_hook(repo: Path) -> None:
    result = runner.invoke(app, ["install-hook", "--root", str(repo)])
    assert result.exit_code == 0
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    assert "canary precommit" in hook.read_text(encoding="utf-8")


def test_cli_install_hook_requires_force_when_present(repo: Path) -> None:
    install_hook(repo)
    result = runner.invoke(app, ["install-hook", "--root", str(repo)])
    assert result.exit_code == 1
    result2 = runner.invoke(app, ["install-hook", "--root", str(repo), "--force"])
    assert result2.exit_code == 0
