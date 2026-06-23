"""Pre-commit guardrail for canary-cage.

Two checks run against the *staged* diff:

1. **Canary removal without uproot.** If a staged change removes a
   planted canary's sentinel from the file it lives in (or deletes that
   file outright) and the canary is still recorded in ``state.json``,
   the commit is blocked. The operator is expected to run
   ``canary uproot`` (which removes the payload *and* clears state)
   instead of hand-editing canaries out.

2. **Fired-beacon leakage.** Anything under ``.canary-cage/fired/`` or
   ``.canary-cage/beacon.log`` being staged is a hard block — those
   files are forensic evidence of a fire and should never enter the
   repo's history.

The check is pure-stdlib: it shells out to ``git`` to read the staged
index. It is invoked by ``canary precommit`` and wired up by
``canary install-hook``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .scanner import _SENTINELS
from .state import STATE_DIR_NAME, PlantedCanary, load_state

FIRED_REL = f"{STATE_DIR_NAME}/fired/"
BEACON_LOG_REL = f"{STATE_DIR_NAME}/beacon.log"


@dataclass(frozen=True)
class PrecommitViolation:
    kind: str  # "canary-removed" | "canary-deleted" | "fired-leak"
    path: str
    detail: str


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _staged_files(root: Path) -> list[tuple[str, str]]:
    """Return ``[(status, path), ...]`` for staged entries.

    ``status`` is the short git status code (e.g. ``A``, ``M``, ``D``,
    ``R100``). Renames are split into deletion + addition by
    ``--name-status`` only when ``-M`` is omitted; we keep it simple and
    rely on ``--name-status`` which yields ``Rxxx old new`` for renames.
    """

    proc = _git(root, "diff", "--cached", "--name-status", "-z")
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    tokens = proc.stdout.split("\x00")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        status = tok[0]
        if status in ("R", "C") and i + 2 < len(tokens):
            # Rename/copy: status, old, new — track the *new* path for
            # leak checks and the *old* path for canary removal checks.
            old, new = tokens[i + 1], tokens[i + 2]
            out.append(("D", old))
            out.append(("A", new))
            i += 3
        else:
            if i + 1 < len(tokens):
                out.append((status, tokens[i + 1]))
                i += 2
            else:
                i += 1
    return out


def _staged_blob(root: Path, path: str) -> str | None:
    proc = _git(root, "show", f":{path}")
    if proc.returncode != 0:
        return None
    return proc.stdout


def _violations_for_canary(
    root: Path, canary: PlantedCanary, staged: dict[str, str]
) -> list[PrecommitViolation]:
    sentinel = _SENTINELS.get(canary.type)
    if sentinel is None:
        return []
    status = staged.get(canary.path)
    if status is None:
        return []
    if status == "D":
        return [
            PrecommitViolation(
                kind="canary-deleted",
                path=canary.path,
                detail=(
                    f"canary {canary.id} ({canary.type}) lives here — "
                    "run `canary uproot` instead of deleting by hand."
                ),
            )
        ]
    # Added/modified/etc. Read the staged content and check the sentinel.
    blob = _staged_blob(root, canary.path)
    expected = f"{sentinel}{canary.marker}"
    if blob is None or expected not in blob:
        return [
            PrecommitViolation(
                kind="canary-removed",
                path=canary.path,
                detail=(
                    f"canary {canary.id} ({canary.type}) sentinel missing "
                    "from staged version — run `canary uproot` first."
                ),
            )
        ]
    return []


def _fired_leak_violations(staged: list[tuple[str, str]]) -> list[PrecommitViolation]:
    out: list[PrecommitViolation] = []
    for status, path in staged:
        if status == "D":
            continue
        norm = path.replace("\\", "/")
        if norm.startswith(FIRED_REL) or norm == BEACON_LOG_REL:
            out.append(
                PrecommitViolation(
                    kind="fired-leak",
                    path=path,
                    detail=(
                        "fired-beacon artifact must not be committed — "
                        "investigate the fire and `git restore --staged` this path."
                    ),
                )
            )
    return out


def check_staged(root: Path) -> list[PrecommitViolation]:
    """Return any pre-commit violations for the staged diff at ``root``."""

    if not (root / ".git").exists():
        return []
    staged_entries = _staged_files(root)
    if not staged_entries:
        return []

    # For canary-removal checks we want the *latest* status per path
    # (renames already split into D + A above).
    latest: dict[str, str] = {}
    for status, path in staged_entries:
        latest[path] = status

    violations: list[PrecommitViolation] = []
    state = load_state(root)
    for canary in state.canaries:
        violations.extend(_violations_for_canary(root, canary, latest))
    violations.extend(_fired_leak_violations(staged_entries))
    return violations


HOOK_SCRIPT = """#!/usr/bin/env sh
# Managed by canary-cage — see `canary install-hook --help`.
exec canary precommit
"""


def install_hook(root: Path, *, force: bool = False) -> Path:
    """Install ``.git/hooks/pre-commit`` that runs ``canary precommit``.

    Raises ``FileExistsError`` if a hook already exists and ``force`` is
    False. Raises ``FileNotFoundError`` if ``root`` is not a git repo.
    """

    git_dir = root / ".git"
    if not git_dir.exists():
        raise FileNotFoundError(f"not a git repo: {root}")
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "pre-commit"
    if target.exists() and not force:
        raise FileExistsError(str(target))
    target.write_text(HOOK_SCRIPT, encoding="utf-8")
    target.chmod(0o755)
    return target
