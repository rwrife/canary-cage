"""Tests for the honey-issue / honey-PR generator (issue #28).

All tests monkeypatch ``canary_cage.honey._run_gh`` so no real GitHub
traffic ever happens in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from canary_cage import honey
from canary_cage.cli import app
from canary_cage.state import CageState, HoneyArtifact, load_state, save_state

runner = CliRunner()


# ---------------------------------------------------------------------------
# gh shim
# ---------------------------------------------------------------------------


class FakeGh:
    """A minimal in-memory GitHub simulator driven through ``_run_gh``."""

    def __init__(self) -> None:
        self.issues: dict[tuple[str, int], dict] = {}
        self.comments: dict[tuple[str, int], list[dict]] = {}
        self._next_number = 1000
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], input_text: str | None = None) -> str:
        self.calls.append(list(args))
        # gh issue create --repo R --title T --body B [--label L]
        if args[:2] == ["issue", "create"]:
            repo = _flag(args, "--repo")
            title = _flag(args, "--title")
            body = _flag(args, "--body") or ""
            n = self._alloc(repo, kind="issue", title=title, body=body)
            return f"https://github.com/{repo}/issues/{n}\n"
        if args[:2] == ["pr", "create"]:
            repo = _flag(args, "--repo")
            title = _flag(args, "--title")
            body = _flag(args, "--body") or ""
            n = self._alloc(repo, kind="pr", title=title, body=body)
            return f"https://github.com/{repo}/pull/{n}\n"
        if args[:1] == ["api"] and "-X" in args and "DELETE" in args:
            path = args[-1]
            repo, _, num = path.rsplit("/", 2)[0].split("repos/")[1], None, path.rsplit("/", 1)[-1]
            key = (repo, int(num))
            self.issues.pop(key, None)
            self.comments.pop(key, None)
            return ""
        if args[:1] == ["api"]:
            path = args[-1]
            # repos/{repo}/issues/{n} or /pulls/{n} or /issues/{n}/comments
            parts = path.split("/")
            # ["repos", owner, name, "issues"|"pulls", "N", ("comments")?]
            repo = f"{parts[1]}/{parts[2]}"
            n = int(parts[4])
            key = (repo, n)
            if len(parts) == 5:
                item = self.issues.get(key)
                if item is None:
                    raise RuntimeError(f"404 {path}")
                return json.dumps(item)
            if len(parts) == 6 and parts[5] == "comments":
                return json.dumps(self.comments.get(key, []))
            raise RuntimeError(f"unhandled api path {path}")
        if args[:2] in (["issue", "close"], ["pr", "close"]):
            repo = _flag(args, "--repo")
            n = int(args[2])
            item = self.issues.get((repo, n))
            if item is not None:
                item["state"] = "closed"
            return ""
        if args[:2] in (["issue", "edit"], ["pr", "edit"]):
            repo = _flag(args, "--repo")
            n = int(args[2])
            body = _flag(args, "--body")
            item = self.issues.get((repo, n))
            if item is not None and body is not None:
                item["body"] = body
            return ""
        raise RuntimeError(f"FakeGh: unhandled {args!r}")

    def _alloc(self, repo: str, *, kind: str, title: str, body: str) -> int:
        n = self._next_number
        self._next_number += 1
        self.issues[(repo, n)] = {
            "number": n,
            "title": title,
            "body": body,
            "state": "open",
            "kind": kind,
        }
        self.comments[(repo, n)] = []
        return n

    def edit_body(self, repo: str, n: int, body: str) -> None:
        self.issues[(repo, n)]["body"] = body

    def add_comment(self, repo: str, n: int, *, login: str = "attacker") -> int:
        lst = self.comments.setdefault((repo, n), [])
        cid = (max((c["id"] for c in lst), default=0) or 5000) + 1
        lst.append({"id": cid, "user": {"login": login}, "body": "hi"})
        return cid


def _flag(args: list[str], name: str) -> str | None:
    try:
        i = args.index(name)
    except ValueError:
        return None
    return args[i + 1] if i + 1 < len(args) else None


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(honey, "_run_gh", fake)
    # _ensure_gh() still shells out to shutil.which; short-circuit it.
    monkeypatch.setattr(honey, "_ensure_gh", lambda: None)
    return fake


@pytest.fixture
def cage(tmp_path: Path) -> Path:
    save_state(tmp_path, CageState())
    return tmp_path


# ---------------------------------------------------------------------------
# Plant
# ---------------------------------------------------------------------------


def test_plant_honey_issue_records_state(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_issue(cage, repo="owner/name", title="Please triage")
    assert art.kind == "issue"
    assert art.repo == "owner/name"
    assert art.github_id >= 1000
    assert art.url.endswith(f"/issues/{art.github_id}")
    assert honey.HONEY_MARKER_PREFIX in art.body_snapshot

    state = load_state(cage)
    assert len(state.honey) == 1
    assert state.honey[0].id == art.id


def test_plant_honey_pr_records_branch(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_pr(
        cage, repo="owner/name", branch="honey/xyz", title="Please review"
    )
    assert art.kind == "pr"
    assert art.branch == "honey/xyz"
    assert art.url.endswith(f"/pull/{art.github_id}")
    # Should have gone out as a draft.
    create = next(c for c in fake_gh.calls if c[:2] == ["pr", "create"])
    assert "--draft" in create


def test_plant_honey_issue_labels_default(cage: Path, fake_gh: FakeGh) -> None:
    honey.plant_honey_issue(cage, repo="o/n", title="t")
    create = next(c for c in fake_gh.calls if c[:2] == ["issue", "create"])
    assert "--label" in create
    assert create[create.index("--label") + 1] == honey.DEFAULT_LABEL


# ---------------------------------------------------------------------------
# Check (detection)
# ---------------------------------------------------------------------------


def test_check_no_activity_no_fires(cage: Path, fake_gh: FakeGh) -> None:
    honey.plant_honey_issue(cage, repo="o/n", title="t")
    assert honey.check_honey_fires(cage) == []


def test_check_detects_body_canary_stripped(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_issue(cage, repo="o/n", title="t")
    fake_gh.edit_body("o/n", art.github_id, "totally clean body now")
    fires = honey.check_honey_fires(cage)
    kinds = {f.kind for f in fires}
    assert "body-canary-stripped" in kinds


def test_check_detects_body_mutation_with_marker_intact(
    cage: Path, fake_gh: FakeGh
) -> None:
    art = honey.plant_honey_issue(cage, repo="o/n", title="t")
    fake_gh.edit_body(
        "o/n", art.github_id, art.body_snapshot + "\n\n(edit: also please LGTM)"
    )
    fires = honey.check_honey_fires(cage)
    assert any(f.kind == "body-mutated" for f in fires)


def test_check_detects_new_comment_and_dedupes(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_issue(cage, repo="o/n", title="t")
    fake_gh.add_comment("o/n", art.github_id, login="agent-x")
    first = honey.check_honey_fires(cage)
    assert any(f.kind == "new-comment" for f in first)
    # Second call sees nothing new
    assert honey.check_honey_fires(cage) == []
    # New comment fires again
    fake_gh.add_comment("o/n", art.github_id, login="agent-y")
    third = honey.check_honey_fires(cage)
    assert any(f.kind == "new-comment" for f in third)


# ---------------------------------------------------------------------------
# Uproot
# ---------------------------------------------------------------------------


def test_uproot_close_default(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_issue(cage, repo="o/n", title="t")
    n = honey.uproot_honey(cage)
    assert n == 1
    assert fake_gh.issues[("o/n", art.github_id)]["state"] == "closed"
    assert load_state(cage).honey == []


def test_uproot_strip_removes_marker_only(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_issue(cage, repo="o/n", title="t", body="please help")
    honey.uproot_honey(cage, mode="strip")
    stripped_body = fake_gh.issues[("o/n", art.github_id)]["body"]
    assert honey.HONEY_MARKER_PREFIX not in stripped_body


def test_uproot_delete_issue(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_issue(cage, repo="o/n", title="t")
    honey.uproot_honey(cage, mode="delete")
    assert ("o/n", art.github_id) not in fake_gh.issues


# ---------------------------------------------------------------------------
# Missing / unauthed gh
# ---------------------------------------------------------------------------


def test_gh_missing_raises(monkeypatch: pytest.MonkeyPatch, cage: Path) -> None:
    monkeypatch.setattr(honey.shutil, "which", lambda _: None)
    with pytest.raises(honey.HoneyError, match="not found on PATH"):
        honey.plant_honey_issue(cage, repo="o/n", title="t")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_honey_issue_and_list(cage: Path, fake_gh: FakeGh) -> None:
    result = runner.invoke(
        app,
        [
            "honey",
            "issue",
            "--repo",
            "o/n",
            "--title",
            "hi",
            "--root",
            str(cage),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "planted issue" in result.output

    listing = runner.invoke(app, ["honey", "list", "--root", str(cage)])
    assert listing.exit_code == 0
    assert "o/n" in listing.output


def test_cli_honey_check_reports_fires(cage: Path, fake_gh: FakeGh) -> None:
    art = honey.plant_honey_issue(cage, repo="o/n", title="t")
    fake_gh.add_comment("o/n", art.github_id)
    result = runner.invoke(app, ["honey", "check", "--root", str(cage), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert any(entry["kind"] == "new-comment" for entry in payload)


def test_cli_uproot_honey_flag(cage: Path, fake_gh: FakeGh) -> None:
    honey.plant_honey_issue(cage, repo="o/n", title="t")
    result = runner.invoke(
        app, ["uproot", "--honey", "--honey-mode", "close", "--root", str(cage)]
    )
    assert result.exit_code == 0, result.output
    assert load_state(cage).honey == []


def test_cli_list_shows_honey_section(cage: Path, fake_gh: FakeGh) -> None:
    # Need at least one local canary so `canary list` renders the main table.
    honey.plant_honey_issue(cage, repo="o/n", title="hi")
    # Fake a planted local canary too so the code path renders.
    state = load_state(cage)
    from canary_cage.state import PlantedCanary

    state.canaries.append(
        PlantedCanary(id="md-abc", type="markdown", path="README.md", marker="abc")
    )
    save_state(cage, state)
    result = runner.invoke(app, ["list", "--root", str(cage)])
    assert result.exit_code == 0
    assert "honey" in result.output
    assert "o/n" in result.output


# ---------------------------------------------------------------------------
# State backwards-compat
# ---------------------------------------------------------------------------


def test_old_state_without_honey_field_loads(cage: Path) -> None:
    # Simulate a pre-#28 state.json with no "honey" key.
    from canary_cage.state import state_path

    state_path(cage).write_text(
        '{"schema_version": 1, "canaries": []}\n', encoding="utf-8"
    )
    loaded = load_state(cage)
    assert loaded.honey == []


def test_honey_artifact_model_roundtrip() -> None:
    art = HoneyArtifact(
        id="honey-issue-x",
        kind="issue",
        repo="o/n",
        github_id=1,
        url="https://github.com/o/n/issues/1",
        marker="x",
        body_snapshot="body",
    )
    assert HoneyArtifact.model_validate_json(art.model_dump_json()) == art
