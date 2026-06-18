"""Scanner — decide whether any canary fired.

Three signals:

1. **Working-tree drift** — a planted canary's sentinel has been removed
   from the file we planted it in (the bird stopped singing → something
   rewrote the file, plausibly an agent acting on the payload).
2. **Stray fire files** — anything matching ``.canary-cage/fired/<id>``
   that wasn't put there by us, or a top-level ``canary fired`` artifact
   the docstring canary's payload tells an agent to drop.
3. **Git history grep** — a canary marker (id, URL, or sentinel) appears
   in commit messages or diffs anywhere in the repo history, which means
   the canary id leaked into something it shouldn't.

The scanner is read-only with respect to canaries. Detected fires are
emitted via the configured beacons (default: file + log).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

from .beacons import Beacon, BeaconRecord, FileBeacon, LogBeacon
from .beacons.file import FIRED_DIR_NAME
from .state import STATE_DIR_NAME, PlantedCanary, load_state

# Canary type → sentinel substring we expect to still be present in the
# planted file. Matches the MARKER_PREFIX used by each canary module.
_SENTINELS = {
    "markdown": "canary-cage:md:BEGIN:",
    "docstring": "canary-cage:py:BEGIN:",
    "todo": "canary-cage:todo:BEGIN:",
}


def default_beacons() -> list[Beacon]:
    return [FileBeacon(), LogBeacon()]


def _check_working_tree(
    root: Path, canary: PlantedCanary
) -> BeaconRecord | None:
    sentinel = _SENTINELS.get(canary.type)
    if sentinel is None:
        return None
    target = root / canary.path
    if not target.exists():
        return BeaconRecord(
            canary_id=canary.id,
            canary_type=canary.type,
            source="working-tree",
            detail=f"planted file vanished: {canary.path}",
            path=canary.path,
        )
    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return BeaconRecord(
            canary_id=canary.id,
            canary_type=canary.type,
            source="working-tree",
            detail=f"could not read {canary.path}: {exc}",
            path=canary.path,
        )
    expected = f"{sentinel}{canary.marker}"
    if expected not in content:
        return BeaconRecord(
            canary_id=canary.id,
            canary_type=canary.type,
            source="working-tree",
            detail=f"sentinel missing from {canary.path}",
            path=canary.path,
        )
    return None


def _check_stray_fire_files(
    root: Path, canaries: Iterable[PlantedCanary]
) -> list[BeaconRecord]:
    fired_dir = root / STATE_DIR_NAME / FIRED_DIR_NAME
    out: list[BeaconRecord] = []
    known_ids = {c.id for c in canaries}
    by_marker = {c.marker: c for c in canaries}

    # An agent that follows the docstring payload writes a file named
    # after the marker. Flag any such file.
    for marker, canary in by_marker.items():
        suspect = fired_dir / marker
        if suspect.exists():
            out.append(
                BeaconRecord(
                    canary_id=canary.id,
                    canary_type=canary.type,
                    source="stray-file",
                    detail=f"agent dropped fire artifact at {suspect.relative_to(root)}",
                    path=str(suspect.relative_to(root)),
                )
            )

    # Also surface stray *.json files inside fired/ that don't map to a
    # known canary id — those were written by something other than us.
    if fired_dir.exists():
        for f in sorted(fired_dir.glob("*.json")):
            stem = f.stem
            if stem not in known_ids:
                out.append(
                    BeaconRecord(
                        canary_id=stem,
                        canary_type="unknown",
                        source="stray-file",
                        detail=f"unknown fire record at {f.relative_to(root)}",
                        path=str(f.relative_to(root)),
                    )
                )
    return out


def _git_log_grep(root: Path, needle: str) -> str | None:
    """Return the first matching commit sha for ``needle`` in git log, or None."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "--all", f"-S{needle}", "--format=%H", "-n", "1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip().splitlines()
    return sha[0] if sha else None


def _check_git_history(
    root: Path, canaries: Iterable[PlantedCanary]
) -> list[BeaconRecord]:
    out: list[BeaconRecord] = []
    if not (root / ".git").exists():
        return out
    for canary in canaries:
        # Grep specifically for the marker token; commits that *introduced*
        # the canary will also match, but the canary id leaking elsewhere is
        # exactly what we want to know about — surfacing both is fine for M4
        # and the operator can ignore the introducing commit.
        sha = _git_log_grep(root, canary.marker)
        if sha:
            out.append(
                BeaconRecord(
                    canary_id=canary.id,
                    canary_type=canary.type,
                    source="git-history",
                    detail=f"marker {canary.marker} appears in commit {sha[:12]}",
                )
            )
    return out


def scan(root: Path, beacons: Iterable[Beacon] | None = None) -> list[BeaconRecord]:
    """Run all signal checks and fire beacons for each detected event."""
    sinks = list(beacons) if beacons is not None else default_beacons()
    state = load_state(root)
    fires: list[BeaconRecord] = []

    for canary in state.canaries:
        rec = _check_working_tree(root, canary)
        if rec is not None:
            fires.append(rec)

    fires.extend(_check_stray_fire_files(root, state.canaries))
    fires.extend(_check_git_history(root, state.canaries))

    for rec in fires:
        for sink in sinks:
            sink.fire(root, rec)
    return fires
