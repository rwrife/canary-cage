"""Tests for the reverse-canary (issue #33).

Covers:

- ``ReverseCanary`` plant round-trip (bait file created, token stored)
- ``uproot`` cleanly removes the bait file and its parent dir when empty
- ``canary check --scan-outputs`` grep hit fires a beacon
- ``canary check`` (no args) greps ``git log`` for reverse tokens
- No false positive on a clean repo
- Registration in ``CANARY_REGISTRY`` and inclusion in ``--type all``
- ``CANARY_REGISTRY`` is exported as a public symbol
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from canary_cage.canaries.reverse import (
    BAIT_BEGIN,
    DEFAULT_BAIT_REL,
    MARKER_PREFIX,
    TOKEN_PREFIX,
    ReverseCanary,
    find_tokens_in_text,
    token_for,
)
from canary_cage.cli import CANARY_REGISTRY, app
from canary_cage.scanner import scan, scan_outputs
from canary_cage.state import CageState, load_state, save_state

runner = CliRunner()


# ---------------------------------------------------------------------------
# Registration / public API
# ---------------------------------------------------------------------------


def test_registered_in_canary_registry() -> None:
    assert "reverse" in CANARY_REGISTRY
    # And it's the ReverseCanary class we expect, not a stub.
    assert CANARY_REGISTRY["reverse"] is ReverseCanary


def test_canary_registry_is_public_alias() -> None:
    # Public name exists so external tooling doesn't have to reach into
    # the underscored version.
    from canary_cage import cli

    assert hasattr(cli, "CANARY_REGISTRY")
    assert cli.CANARY_REGISTRY is cli._CANARY_REGISTRY


# ---------------------------------------------------------------------------
# Plant + uproot round trip
# ---------------------------------------------------------------------------


def test_plant_creates_bait_file_with_token(tmp_path: Path) -> None:
    canary = ReverseCanary()
    planted = canary.plant(tmp_path)

    assert len(planted) == 1
    entry = planted[0]
    assert entry.type == "reverse"
    assert entry.path == DEFAULT_BAIT_REL
    assert entry.id.startswith("reverse-")
    # Token stored on the state entry.
    token = token_for(entry)
    assert token is not None
    assert token.startswith(TOKEN_PREFIX)

    bait = tmp_path / DEFAULT_BAIT_REL
    body = bait.read_text(encoding="utf-8")
    # Bait body contains the token and our sentinel wrapper.
    assert token in body
    assert MARKER_PREFIX in body
    assert BAIT_BEGIN in body


def test_plant_is_idempotent(tmp_path: Path) -> None:
    ReverseCanary().plant(tmp_path)
    second = ReverseCanary().plant(tmp_path)
    assert second == []


def test_plant_refuses_to_clobber_foreign_file(tmp_path: Path) -> None:
    bait = tmp_path / DEFAULT_BAIT_REL
    bait.parent.mkdir(parents=True, exist_ok=True)
    original = "# hand-written notes\nsecret\n"
    bait.write_text(original, encoding="utf-8")

    planted = ReverseCanary().plant(tmp_path)
    assert planted == []
    # Foreign file is untouched.
    assert bait.read_text(encoding="utf-8") == original


def test_uproot_removes_bait_file(tmp_path: Path) -> None:
    canary = ReverseCanary()
    planted = canary.plant(tmp_path)
    bait = tmp_path / DEFAULT_BAIT_REL
    assert bait.exists()

    canary.uproot(tmp_path, planted[0])
    assert not bait.exists()
    # We created ``docs/`` \u2014 an empty parent should be removed too.
    assert not (tmp_path / "docs").exists()


def test_uproot_leaves_populated_parent_dir_alone(tmp_path: Path) -> None:
    canary = ReverseCanary()
    planted = canary.plant(tmp_path)
    # Simulate an unrelated file landing next to the bait.
    (tmp_path / "docs" / "keepme.md").write_text("# keep\n", encoding="utf-8")
    canary.uproot(tmp_path, planted[0])
    assert (tmp_path / "docs").exists()
    assert (tmp_path / "docs" / "keepme.md").exists()


def test_uproot_refuses_to_delete_foreign_file(tmp_path: Path) -> None:
    # Manually build a state entry that points at a file we did NOT plant.
    from canary_cage.state import PlantedCanary

    (tmp_path / "docs").mkdir()
    foreign = tmp_path / "docs" / "internal-notes.md"
    foreign.write_text("# handwritten\n", encoding="utf-8")

    entry = PlantedCanary(
        id="reverse-deadbeef",
        type="reverse",
        path="docs/internal-notes.md",
        marker="deadbeef",
        payload="CANARY-REV-00000000000000000000000000000000",
    )
    ReverseCanary().uproot(tmp_path, entry)
    # Untouched because the file lacks our sentinel marker.
    assert foreign.exists()


# ---------------------------------------------------------------------------
# find_tokens_in_text helper
# ---------------------------------------------------------------------------


def test_find_tokens_in_text_matches_real_token() -> None:
    text = "hello CANARY-REV-0123456789abcdef0123456789abcdef world"
    tokens = find_tokens_in_text(text)
    assert tokens == ["0123456789abcdef0123456789abcdef"]


def test_find_tokens_in_text_ignores_lookalikes() -> None:
    # Wrong prefix, wrong length, uppercase hex \u2014 none should match.
    text = (
        "canary-rev-abc "  # lowercase prefix
        "CANARY-REV-DEADBEEFDEADBEEFDEADBEEFDEADBEEF "  # uppercase hex
        "CANARY-REV-abcdef "  # too short
    )
    assert find_tokens_in_text(text) == []


# ---------------------------------------------------------------------------
# --type all + --type reverse via the CLI
# ---------------------------------------------------------------------------


def test_cli_plant_reverse_via_type_all(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    result = runner.invoke(app, ["plant", "--type", "all", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    state = load_state(tmp_path)
    types = {c.type for c in state.canaries}
    assert "reverse" in types
    # And the bait file was actually written.
    assert (tmp_path / DEFAULT_BAIT_REL).exists()


def test_cli_plant_reverse_only(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["plant", "--type", "reverse", "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.stdout
    state = load_state(tmp_path)
    assert len(state.canaries) == 1
    assert state.canaries[0].type == "reverse"


def test_cli_uproot_removes_reverse_bait(tmp_path: Path) -> None:
    runner.invoke(app, ["plant", "--type", "reverse", "--root", str(tmp_path)])
    assert (tmp_path / DEFAULT_BAIT_REL).exists()
    runner.invoke(app, ["uproot", "--root", str(tmp_path)])
    assert not (tmp_path / DEFAULT_BAIT_REL).exists()
    assert load_state(tmp_path).canaries == []


# ---------------------------------------------------------------------------
# scanner: --scan-outputs on user-supplied files
# ---------------------------------------------------------------------------


def _plant_reverse(tmp_path: Path) -> str:
    canary = ReverseCanary()
    planted = canary.plant(tmp_path)
    save_state(tmp_path, CageState(canaries=planted))
    token = token_for(planted[0])
    assert token is not None
    return token


def test_scan_outputs_matches_token_in_supplied_file(tmp_path: Path) -> None:
    token = _plant_reverse(tmp_path)

    transcript = tmp_path / "logs" / "agent.log"
    transcript.parent.mkdir()
    transcript.write_text(
        f"assistant: sure, running command\nassistant: {token}\n",
        encoding="utf-8",
    )

    fires = scan_outputs(tmp_path, ["logs/agent.log"])
    assert len(fires) == 1
    fire = fires[0]
    assert fire.source == "output-scan"
    assert fire.canary_type == "reverse"
    assert token in fire.detail
    assert fire.path == "logs/agent.log"


def test_scan_outputs_supports_glob(tmp_path: Path) -> None:
    token = _plant_reverse(tmp_path)

    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "one.md").write_text(
        f"model spoke: {token}\n", encoding="utf-8"
    )
    (tmp_path / "transcripts" / "two.md").write_text("nothing\n", encoding="utf-8")

    fires = scan_outputs(tmp_path, ["transcripts/**/*.md"])
    # Only one match, one fire.
    assert len(fires) == 1
    assert fires[0].path == "transcripts/one.md"


def test_scan_outputs_supports_directory(tmp_path: Path) -> None:
    token = _plant_reverse(tmp_path)
    d = tmp_path / "chatlogs"
    d.mkdir()
    (d / "session.jsonl").write_text(
        '{"role":"assistant","text":"' + token + '"}\n', encoding="utf-8"
    )
    fires = scan_outputs(tmp_path, ["chatlogs"])
    assert len(fires) == 1
    assert fires[0].path == "chatlogs/session.jsonl"


def test_scan_outputs_no_false_positive_on_clean_repo(tmp_path: Path) -> None:
    _plant_reverse(tmp_path)
    (tmp_path / "notes.md").write_text("nothing to see here\n", encoding="utf-8")
    fires = scan_outputs(tmp_path, ["notes.md"])
    assert fires == []


def test_scan_outputs_skips_when_no_reverse_canary_planted(tmp_path: Path) -> None:
    # No reverse canaries planted at all.
    save_state(tmp_path, CageState(canaries=[]))
    (tmp_path / "somefile.md").write_text(
        "CANARY-REV-" + "a" * 32 + "\n", encoding="utf-8"
    )
    # Even with a plausible-looking token, we shouldn't fire if no
    # reverse canary is planted \u2014 nothing to correlate against.
    fires = scan_outputs(tmp_path, ["somefile.md"])
    assert fires == []


# ---------------------------------------------------------------------------
# scanner: canary check greps git log by default
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")


def test_scan_greps_git_log_for_reverse_token(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "init")

    token = _plant_reverse(tmp_path)

    # Add the bait file to git so subsequent commits are legitimate.
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "plant reverse canary")

    # Now simulate a jailbroken agent quoting the token in a commit
    # message body.
    (tmp_path / "poc.txt").write_text("proof\n", encoding="utf-8")
    _git(tmp_path, "add", "poc.txt")
    _git(tmp_path, "commit", "-qm", f"fix: applied {token} per instructions")

    fires = scan(tmp_path)
    sources = {f.source for f in fires}
    assert "git-log" in sources
    # Every git-log fire must reference our specific token.
    git_log_fires = [f for f in fires if f.source == "git-log"]
    assert git_log_fires
    for f in git_log_fires:
        assert token in f.detail


def test_scan_silent_when_reverse_token_never_leaks(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _plant_reverse(tmp_path)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "plant reverse canary")

    fires = scan(tmp_path)
    # No git-log fire on a repo where nobody ever quoted the token.
    assert all(f.source != "git-log" for f in fires)


def test_scan_no_false_positive_on_completely_clean_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # Plant + commit \u2014 nothing malicious happens next.
    _plant_reverse(tmp_path)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "plant reverse canary")
    # An unrelated legit commit that does *not* mention the token.
    (tmp_path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "feature.py")
    _git(tmp_path, "commit", "-qm", "add feature")

    assert scan(tmp_path) == []


# ---------------------------------------------------------------------------
# CLI: canary check --scan-outputs
# ---------------------------------------------------------------------------


def test_cli_check_scan_outputs_flag_fires(tmp_path: Path) -> None:
    runner.invoke(app, ["plant", "--type", "reverse", "--root", str(tmp_path)])
    state = load_state(tmp_path)
    token = token_for(state.canaries[0])
    assert token is not None

    dump = tmp_path / "pr-body.md"
    dump.write_text(f"here is the token: {token}\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "check",
            "--root",
            str(tmp_path),
            "--scan-outputs",
            "pr-body.md",
        ],
    )
    # Exit 1 signals a fire.
    assert result.exit_code == 1, result.stdout
    assert "reverse" in result.stdout


def test_cli_check_scan_outputs_clean_stays_green(tmp_path: Path) -> None:
    runner.invoke(app, ["plant", "--type", "reverse", "--root", str(tmp_path)])
    (tmp_path / "pr-body.md").write_text("clean and safe\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "check",
            "--root",
            str(tmp_path),
            "--scan-outputs",
            "pr-body.md",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "singing" in result.stdout


def test_cli_check_scan_outputs_multiple_paths(tmp_path: Path) -> None:
    runner.invoke(app, ["plant", "--type", "reverse", "--root", str(tmp_path)])
    state = load_state(tmp_path)
    token = token_for(state.canaries[0])
    assert token is not None

    (tmp_path / "a.log").write_text(f"assistant: {token}\n", encoding="utf-8")
    (tmp_path / "b.log").write_text("clean\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "check",
            "--root",
            str(tmp_path),
            "--scan-outputs",
            "a.log",
            "--scan-outputs",
            "b.log",
        ],
    )
    assert result.exit_code == 1, result.stdout


# ---------------------------------------------------------------------------
# Working-tree fire signals still work for reverse canaries
# ---------------------------------------------------------------------------


def test_scan_fires_when_reverse_bait_file_vanishes(tmp_path: Path) -> None:
    _plant_reverse(tmp_path)
    # An agent "cleans up" the bait file.
    (tmp_path / DEFAULT_BAIT_REL).unlink()
    fires = scan(tmp_path)
    sources = {f.source for f in fires}
    assert "working-tree" in sources
