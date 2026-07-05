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

from .beacons import (
    Beacon,
    BeaconRecord,
    DiscordBeacon,
    FileBeacon,
    LogBeacon,
    OtelBeacon,
    SlackBeacon,
    WebhookBeacon,
)
from .beacons.file import FIRED_DIR_NAME
from .canaries.reverse import token_for
from .config import load_config
from .state import STATE_DIR_NAME, PlantedCanary, load_state

# Canary type → sentinel substring we expect to still be present in the
# planted file. Matches the MARKER_PREFIX used by each canary module.
_SENTINELS = {
    "markdown": "canary-cage:md:BEGIN:",
    "docstring": "canary-cage:py:BEGIN:",
    "todo": "canary-cage:todo:BEGIN:",
    "manifest": "canary-cage:manifest:BEGIN:",
    "reverse": "canary-cage:reverse:BEGIN:",
}

# Lockfiles to scan for typosquat-trap package mentions. The presence of
# a fake canary-trip-<marker> package in any of these is a strong signal
# that an agent tried to resolve/install the trap.
_LOCKFILES: tuple[str, ...] = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.lock",
    "requirements.txt.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)


def default_beacons() -> list[Beacon]:
    return [FileBeacon(), LogBeacon()]


def beacons_for(root: Path) -> list[Beacon]:
    """Default beacons plus any beacons enabled in ``canary.toml``."""
    sinks: list[Beacon] = default_beacons()
    try:
        cfg = load_config(root)
    except (ValueError, OSError):
        return sinks
    if cfg.webhook.url:
        sinks.append(
            WebhookBeacon(
                url=cfg.webhook.url,
                timeout=cfg.webhook.timeout,
                max_attempts=cfg.webhook.max_attempts,
                backoff=cfg.webhook.backoff,
                headers=dict(cfg.webhook.headers),
            )
        )
    if cfg.slack.url:
        sinks.append(
            SlackBeacon(
                url=cfg.slack.url,
                timeout=cfg.slack.timeout,
                max_attempts=cfg.slack.max_attempts,
                backoff=cfg.slack.backoff,
                headers=dict(cfg.slack.headers),
                snippet_chars=cfg.slack.snippet_chars,
            )
        )
    if cfg.discord.url:
        sinks.append(
            DiscordBeacon(
                url=cfg.discord.url,
                timeout=cfg.discord.timeout,
                max_attempts=cfg.discord.max_attempts,
                backoff=cfg.discord.backoff,
                headers=dict(cfg.discord.headers),
                snippet_chars=cfg.discord.snippet_chars,
            )
        )
    if cfg.otel.enabled:
        sinks.append(
            OtelBeacon(
                enabled=True,
                service_name=cfg.otel.service_name,
                resource_attributes=dict(cfg.otel.resource_attributes),
            )
        )
    return sinks


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


def _git_log_messages(root: Path) -> str | None:
    """Return the full concatenation of all commit *messages* (``%B``).

    Distinct from :func:`_git_log_grep`, which searches diffs for a
    needle. Reverse-canary tokens most commonly leak into commit
    *messages* (an agent narrating its work), so we grep the message
    body directly.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "--all", "--format=%H%n%B%n---"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _check_git_history(
    root: Path, canaries: Iterable[PlantedCanary]
) -> list[BeaconRecord]:
    out: list[BeaconRecord] = []
    if not (root / ".git").exists():
        return out
    for canary in canaries:
        # Reverse-canaries are covered by ``_check_reverse_git_log`` which
        # greps commit *message bodies* for the token — running a
        # ``-S<marker>`` diff grep here would just re-flag the commit that
        # originally planted the bait file. Skip.
        if canary.type == "reverse":
            continue
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


def _check_lockfile_mentions(
    root: Path, canaries: Iterable[PlantedCanary]
) -> list[BeaconRecord]:
    """Flag any manifest canary whose fake package shows up in a lockfile."""
    out: list[BeaconRecord] = []
    manifest_canaries = [c for c in canaries if c.type == "manifest"]
    if not manifest_canaries:
        return out
    for name in _LOCKFILES:
        for lock_path in sorted(root.glob(f"**/{name}")):
            try:
                rel = lock_path.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            try:
                text = lock_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for canary in manifest_canaries:
                needle = f"canary-trip-{canary.marker}"
                if needle in text:
                    out.append(
                        BeaconRecord(
                            canary_id=canary.id,
                            canary_type=canary.type,
                            source="lockfile",
                            detail=(
                                f"trap package {needle} appears in {rel} — "
                                "agent likely tried to resolve it."
                            ),
                            path=str(rel),
                        )
                    )
    return out


def _reverse_canaries(
    canaries: Iterable[PlantedCanary],
) -> list[tuple[PlantedCanary, str]]:
    """Return ``(canary, token)`` pairs for every reverse canary with a token."""
    pairs: list[tuple[PlantedCanary, str]] = []
    for c in canaries:
        if c.type != "reverse":
            continue
        token = token_for(c)
        if token:
            pairs.append((c, token))
    return pairs


def _snippet(text: str, needle: str, radius: int = 60) -> str:
    """Return a short context snippet around ``needle`` in ``text``."""
    idx = text.find(needle)
    if idx == -1:
        return needle
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    snippet = text[start:end].replace("\n", " ").strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _check_reverse_git_log(
    root: Path, canaries: Iterable[PlantedCanary]
) -> list[BeaconRecord]:
    """Grep every commit message for reverse-canary tokens.

    Fires when a ``CANARY-REV-<hex>`` token planted in a bait file
    turns up in a commit *message* body. Only messages are scanned
    here — leakage into diffs is out-of-scope for this pass (a naive
    ``-S<token>`` would match the introducing commit that planted the
    bait file itself, which is noise).
    """
    pairs = _reverse_canaries(canaries)
    if not pairs:
        return []
    if not (root / ".git").exists():
        return []
    messages = _git_log_messages(root)
    if not messages:
        return []
    out: list[BeaconRecord] = []
    for canary, token in pairs:
        idx = messages.find(token)
        if idx == -1:
            continue
        # Walk back to the nearest %H line before the match to figure
        # out which commit leaked the token.
        head = messages[:idx]
        sha = ""
        for line in reversed(head.splitlines()):
            line = line.strip()
            if len(line) == 40 and all(ch in "0123456789abcdef" for ch in line):
                sha = line
                break
        detail = (
            f"reverse-canary token {token} appears in commit message"
            + (f" {sha[:12]}" if sha else "")
        )
        out.append(
            BeaconRecord(
                canary_id=canary.id,
                canary_type=canary.type,
                source="git-log",
                detail=detail,
            )
        )
    return out


# File extensions we're willing to scan for reverse-canary tokens.
# Deliberately conservative — grepping every binary in a repo is a
# footgun. Users can widen this via ``canary check --scan-outputs`` on
# an explicit glob.
_SCAN_TEXT_EXTS: frozenset[str] = frozenset(
    {
        ".md",
        ".txt",
        ".log",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".sh",
        ".rb",
        ".diff",
        ".patch",
    }
)

_SCAN_MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file cap; skip larger.


def _iter_scan_targets(root: Path, patterns: Iterable[str]) -> list[Path]:
    """Expand user-supplied paths/globs into a de-duplicated file list.

    Each pattern can be an absolute path, a path relative to ``root``,
    or a glob (``docs/**/*.md``). Directories are recursed into and
    filtered by :data:`_SCAN_TEXT_EXTS`; explicit file paths are always
    included regardless of extension so power users can point at, e.g.,
    an ``agent-transcript.dat`` dump.
    """
    seen: set[Path] = set()
    out: list[Path] = []

    def _add(p: Path, *, extension_check: bool) -> None:
        try:
            resolved = p.resolve()
        except OSError:
            return
        if resolved in seen or not resolved.is_file():
            return
        if extension_check and resolved.suffix.lower() not in _SCAN_TEXT_EXTS:
            return
        seen.add(resolved)
        out.append(resolved)

    for pattern in patterns:
        raw = str(pattern)
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (root / raw).resolve()
        # Explicit file path — include unconditionally.
        if candidate.exists() and candidate.is_file():
            _add(candidate, extension_check=False)
            continue
        if candidate.exists() and candidate.is_dir():
            for f in sorted(candidate.rglob("*")):
                _add(f, extension_check=True)
            continue
        # Fall back to glob semantics rooted at ``root``.
        # Support absolute globs by anchoring at filesystem root.
        try:
            if Path(raw).is_absolute():
                # ``Path.glob`` doesn't accept absolute patterns — split.
                anchor = Path(raw).anchor or "/"
                rel = raw[len(anchor):]
                matches = Path(anchor).glob(rel)
            else:
                matches = root.glob(raw)
        except (NotImplementedError, ValueError):
            continue
        for match in sorted(matches):
            if match.is_file():
                _add(match, extension_check=True)
            elif match.is_dir():
                for f in sorted(match.rglob("*")):
                    _add(f, extension_check=True)
    return out


def scan_outputs(
    root: Path,
    patterns: Iterable[str],
    *,
    beacons: Iterable[Beacon] | None = None,
) -> list[BeaconRecord]:
    """Scan arbitrary text files/globs for reverse-canary tokens.

    Complements :func:`scan` — the latter looks at working-tree state,
    stray-file artifacts, git history, and lockfiles; this one grepss
    caller-supplied *outputs* (agent transcripts, PR body dumps, chat
    logs) for the linguistic fingerprint of a jailbreak.
    """
    sinks = list(beacons) if beacons is not None else beacons_for(root)
    state = load_state(root)
    pairs = _reverse_canaries(c for c in state.canaries if c.is_armed())
    if not pairs:
        return []
    targets = _iter_scan_targets(root, patterns)
    fires: list[BeaconRecord] = []
    for target in targets:
        try:
            if target.stat().st_size > _SCAN_MAX_BYTES:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for canary, token in pairs:
            if token not in text:
                continue
            try:
                rel = str(target.relative_to(root))
            except ValueError:
                rel = str(target)
            fires.append(
                BeaconRecord(
                    canary_id=canary.id,
                    canary_type=canary.type,
                    source="output-scan",
                    detail=(
                        f"reverse-canary token {token} found in {rel}: "
                        + _snippet(text, token)
                    ),
                    path=rel,
                )
            )
    for rec in fires:
        for sink in sinks:
            sink.fire(root, rec)
    return fires


def scan(root: Path, beacons: Iterable[Beacon] | None = None) -> list[BeaconRecord]:
    """Run all signal checks and fire beacons for each detected event."""
    sinks = list(beacons) if beacons is not None else beacons_for(root)
    state = load_state(root)
    fires: list[BeaconRecord] = []

    # Dormant (time-bomb) canaries don't fire — they sit silently until
    # their ``armed_at`` passes. Filter them out across every check.
    armed = [c for c in state.canaries if c.is_armed()]

    for canary in armed:
        rec = _check_working_tree(root, canary)
        if rec is not None:
            fires.append(rec)

    fires.extend(_check_stray_fire_files(root, armed))
    fires.extend(_check_git_history(root, armed))
    fires.extend(_check_lockfile_mentions(root, armed))
    fires.extend(_check_reverse_git_log(root, armed))

    for rec in fires:
        for sink in sinks:
            sink.fire(root, rec)
    return fires
