"""`canary diff` — compare current tree against last-planted state.

Where ``canary check`` is a yes/no fire detector, ``diff`` answers the
postmortem question: *what mutated, and where?* It walks every planted
canary, recomputes whether the sentinel block is still intact, and
classifies each one as one of:

- ``intact``   — sentinel block present and unmodified
- ``mutated``  — file still exists, BEGIN sentinel still present, but
  the END sentinel (or surrounding payload) was tampered with
- ``removed``  — file vanished, or the BEGIN sentinel is gone
- ``fired``    — the scanner found independent evidence of a fire (stray
  fire artifact, git-history leak, lockfile mention, etc.)

A ``fired`` verdict outranks the structural verdicts because it's the
strongest signal. The schema below is what the ``--json`` flag emits and
is intended to be stable for CI consumers.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import _SENTINELS, scan
from .state import PlantedCanary, load_state

DIFF_SCHEMA_VERSION = 1

VERDICT_INTACT = "intact"
VERDICT_MUTATED = "mutated"
VERDICT_REMOVED = "removed"
VERDICT_FIRED = "fired"

# Ordering used both for table grouping and for picking a "worst" verdict
# when multiple signals collide on one canary.
_VERDICT_RANK = {
    VERDICT_INTACT: 0,
    VERDICT_MUTATED: 1,
    VERDICT_REMOVED: 2,
    VERDICT_FIRED: 3,
}


@dataclass
class CanaryDiff:
    """Per-canary diff result."""

    canary_id: str
    canary_type: str
    path: str
    verdict: str
    detail: str = ""
    expected: str | None = None
    actual: str | None = None
    fire_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "canary_id": self.canary_id,
            "canary_type": self.canary_type,
            "path": self.path,
            "verdict": self.verdict,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
            "fire_sources": list(self.fire_sources),
        }


@dataclass
class DiffReport:
    """Full diff report — what gets serialized by ``--json``."""

    schema_version: int
    root: str
    since: str | None
    total_planted: int
    diffs: list[CanaryDiff]

    def to_dict(self) -> dict[str, object]:
        by_type: dict[str, list[dict[str, object]]] = {}
        for d in self.diffs:
            by_type.setdefault(d.canary_type, []).append(d.to_dict())
        return {
            "schema_version": self.schema_version,
            "root": self.root,
            "since": self.since,
            "total_planted": self.total_planted,
            "groups": [
                {"canary_type": t, "items": items}
                for t, items in sorted(by_type.items())
            ],
        }

    def summary(self) -> dict[str, int]:
        counts = {v: 0 for v in _VERDICT_RANK}
        for d in self.diffs:
            counts[d.verdict] = counts.get(d.verdict, 0) + 1
        return counts


def _changed_paths_since(root: Path, ref: str) -> set[str] | None:
    """Return repo-relative paths changed since ``ref``, or None on failure."""
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", f"{ref}...HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        # Fall back to plain diff against the ref (covers untracked refs
        # like a tag that lacks a merge-base with HEAD).
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "diff", "--name-only", ref],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _classify(root: Path, canary: PlantedCanary) -> CanaryDiff:
    sentinel = _SENTINELS.get(canary.type)
    target = root / canary.path
    if sentinel is None:
        return CanaryDiff(
            canary_id=canary.id,
            canary_type=canary.type,
            path=canary.path,
            verdict=VERDICT_MUTATED,
            detail=f"unknown canary type {canary.type!r}",
        )
    expected_begin = f"{sentinel}{canary.marker}"
    end_marker = sentinel.replace(":BEGIN:", ":END:") + canary.marker
    if not target.exists():
        return CanaryDiff(
            canary_id=canary.id,
            canary_type=canary.type,
            path=canary.path,
            verdict=VERDICT_REMOVED,
            detail=f"planted file vanished: {canary.path}",
            expected=expected_begin,
            actual=None,
        )
    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return CanaryDiff(
            canary_id=canary.id,
            canary_type=canary.type,
            path=canary.path,
            verdict=VERDICT_REMOVED,
            detail=f"could not read {canary.path}: {exc}",
            expected=expected_begin,
        )
    if expected_begin not in content:
        return CanaryDiff(
            canary_id=canary.id,
            canary_type=canary.type,
            path=canary.path,
            verdict=VERDICT_REMOVED,
            detail=f"BEGIN sentinel missing from {canary.path}",
            expected=expected_begin,
            actual=_snippet_around(content, sentinel.split(":BEGIN:")[0]),
        )
    if end_marker not in content:
        return CanaryDiff(
            canary_id=canary.id,
            canary_type=canary.type,
            path=canary.path,
            verdict=VERDICT_MUTATED,
            detail=f"END sentinel missing — payload tampered in {canary.path}",
            expected=end_marker,
            actual=_snippet_around(content, expected_begin),
        )
    return CanaryDiff(
        canary_id=canary.id,
        canary_type=canary.type,
        path=canary.path,
        verdict=VERDICT_INTACT,
        detail="sentinel block intact",
        expected=expected_begin,
    )


def _snippet_around(content: str, needle: str, radius: int = 80) -> str | None:
    idx = content.find(needle)
    if idx == -1:
        # Fall back to the first non-empty line so the operator sees *something*.
        for line in content.splitlines():
            if line.strip():
                return line[: 2 * radius]
        return None
    start = max(0, idx - radius)
    end = min(len(content), idx + len(needle) + radius)
    return content[start:end]


def compute_diff(root: Path, since: str | None = None) -> DiffReport:
    """Compare the current tree against last-planted state.

    When ``since`` is provided, only canaries whose planted path changed
    in git since that ref are included in the diff.
    """
    state = load_state(root)
    canaries: list[PlantedCanary] = list(state.canaries)

    if since is not None:
        changed = _changed_paths_since(root, since)
        if changed is not None:
            canaries = [c for c in canaries if c.path in changed]

    diffs = [_classify(root, c) for c in canaries]

    # Cross-reference with the scanner so a leaked-marker or stray-fire
    # signal upgrades the verdict to "fired".
    by_id = {d.canary_id: d for d in diffs}
    try:
        fires = scan(root, beacons=[])
    except Exception:  # pragma: no cover — defensive: never let scan crash diff
        fires = []
    for rec in fires:
        d = by_id.get(rec.canary_id)
        if d is None:
            continue
        # The scanner's working-tree check duplicates what _classify already
        # detected. Git-history grep always matches the planting commit, which
        # is not evidence of fire. For diff verdicts, only treat truly
        # independent signals (stray fire artifacts, lockfile mentions) as
        # "fired" — structural verdicts (removed/mutated) survive otherwise.
        if rec.source in ("working-tree", "git-history"):
            continue
        if _VERDICT_RANK[VERDICT_FIRED] > _VERDICT_RANK[d.verdict]:
            d.verdict = VERDICT_FIRED
        if rec.source not in d.fire_sources:
            d.fire_sources.append(rec.source)
        if rec.detail and rec.detail not in d.detail:
            d.detail = (d.detail + " | " if d.detail else "") + rec.detail

    diffs.sort(
        key=lambda d: (-_VERDICT_RANK[d.verdict], d.canary_type, d.path, d.canary_id)
    )
    return DiffReport(
        schema_version=DIFF_SCHEMA_VERSION,
        root=str(root),
        since=since,
        total_planted=len(state.canaries),
        diffs=diffs,
    )
