"""``canary explain`` — human-readable jack report per fire (issue #41).

Turns a single fire (a ``BeaconRecord`` persisted by the file beacon under
``.canary-cage/fired/<canary_id>.json``) into a plain-English incident
report: what tripped, why we think it tripped, a bait-vs-observed diff,
git correlation, a severity score, and a recommended-actions checklist.

This module ships the *core* pieces from the issue #41 acceptance list:

* :func:`load_fire` — read one fire off disk.
* :func:`build_report` — assemble a :class:`Report` from a fire + repo state.
* :func:`score_severity` — pure severity scorer with documented rules.
* :func:`render_markdown` / :func:`render_text` — deterministic renderers.

Explicit follow-ups (tracked in the issue, not implemented here):

* ``--attach-to`` to shell out to ``gh {issue,pr} comment``.
* ``--all`` batch mode.
* LLM-narrated variant.
* Signature-verification block for signed canaries (issue #40).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from .beacons.base import BeaconRecord
from .beacons.file import fired_dir
from .state import PlantedCanary, load_state

Severity = Literal["low", "medium", "high", "critical"]

# Signals the scorer looks at. Weights are intentionally small integers so
# the total maps cleanly onto the four severity buckets and stays easy to
# reason about in tests + docs. Tunable via ``canary.toml`` in a follow-up.
DEFAULT_SEVERITY_WEIGHTS: dict[str, int] = {
    "aggressive_bait": 2,   # bait explicitly asks the agent to run/exfil
    "code_execution": 3,    # scanner attributed the fire to executed code, not just a file write
    "external_url": 2,      # bait contained an outbound URL / webhook
    "near_secrets": 2,      # trip site sits next to obvious secret-looking tokens
    "sensitive_path": 1,    # fired file lives under a sensitive path (.env, secrets/, ci)
}

_SEVERITY_THRESHOLDS: list[tuple[int, Severity]] = [
    (7, "critical"),
    (5, "high"),
    (3, "medium"),
    (0, "low"),
]

_SECRET_HINTS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "AWS_",
    "GITHUB_TOKEN",
)

_SENSITIVE_PATH_HINTS = (
    ".env",
    "secrets/",
    "secret/",
    ".github/workflows",
    "credentials",
    "id_rsa",
)

_EXECUTION_SOURCES = {"output-scan", "git-history", "git-log"}


# ---------------------------------------------------------------------------
# Data classes


@dataclass
class SeverityBreakdown:
    """Result of :func:`score_severity` — score + which signals fired."""

    score: int
    severity: Severity
    signals: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=dict)


@dataclass
class GitCorrelation:
    """Best-effort git-log correlation for the fired path."""

    commit: str | None = None
    author: str | None = None
    committed_at: str | None = None
    subject: str | None = None
    touched_files: list[str] = field(default_factory=list)
    available: bool = False


@dataclass
class Report:
    """Structured incident report for a single fire."""

    fire: BeaconRecord
    canary: PlantedCanary | None
    bait_text: str
    observed_text: str
    diff: list[str]
    git: GitCorrelation
    severity: SeverityBreakdown
    recommendations: list[str]


# ---------------------------------------------------------------------------
# Loading


def load_fire(root: Path, fire_id: str) -> BeaconRecord:
    """Read ``<root>/.canary-cage/fired/<fire_id>.json`` into a record.

    Raises :class:`FileNotFoundError` when no such fire exists so callers
    (the CLI) can turn it into a friendly error message.
    """

    path = fired_dir(root) / f"{fire_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no fire recorded for id {fire_id!r} at {path}")
    return BeaconRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_fires(root: Path) -> list[BeaconRecord]:
    """Return every fire currently persisted under ``.canary-cage/fired/``."""

    directory = fired_dir(root)
    if not directory.exists():
        return []
    out: list[BeaconRecord] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(BeaconRecord.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception:
            # A malformed file shouldn't kill the whole batch report — skip
            # it and let the caller notice via the missing id.
            continue
    return out


# ---------------------------------------------------------------------------
# Severity


def score_severity(
    fire: BeaconRecord,
    canary: PlantedCanary | None,
    bait_text: str,
    observed_text: str,
    *,
    weights: dict[str, int] | None = None,
) -> SeverityBreakdown:
    """Deterministically score a fire.

    Rules (each fires at most once, weights sum into a total):

    * ``aggressive_bait``  — bait text contains an imperative-looking
      exfil/execute keyword (``curl``, ``wget``, ``exfil``, ``rm -rf``,
      ``base64``, ``eval``).
    * ``code_execution``   — the scanner attributed the fire to something
      other than a stray file write (git history, output scan, git log).
    * ``external_url``     — bait text contains an ``http(s)://`` URL.
    * ``near_secrets``     — the *observed* text mentions a secret-looking
      token (``SECRET``, ``TOKEN``, ``API_KEY`` …).
    * ``sensitive_path``   — the fired path lives under a sensitive
      directory (``.env``, ``secrets/``, ``.github/workflows`` …).
    """

    w = {**DEFAULT_SEVERITY_WEIGHTS, **(weights or {})}
    signals: list[str] = []

    bait_lower = bait_text.lower()
    aggressive_markers = ("curl ", "wget ", "exfil", "rm -rf", "base64", "eval(")
    if any(marker in bait_lower for marker in aggressive_markers):
        signals.append("aggressive_bait")

    if fire.source in _EXECUTION_SOURCES:
        signals.append("code_execution")

    if "http://" in bait_text or "https://" in bait_text:
        signals.append("external_url")

    observed_upper = observed_text.upper()
    if any(hint in observed_upper for hint in _SECRET_HINTS):
        signals.append("near_secrets")

    fired_path = (fire.path or (canary.path if canary else "") or "").lower()
    if any(hint in fired_path for hint in _SENSITIVE_PATH_HINTS):
        signals.append("sensitive_path")

    score = sum(w.get(s, 0) for s in signals)
    severity: Severity = "low"
    for threshold, label in _SEVERITY_THRESHOLDS:
        if score >= threshold:
            severity = label
            break

    return SeverityBreakdown(
        score=score,
        severity=severity,
        signals=signals,
        weights={s: w.get(s, 0) for s in signals},
    )


# ---------------------------------------------------------------------------
# Recommendations


def recommend_actions(
    fire: BeaconRecord,
    canary: PlantedCanary | None,
    severity: SeverityBreakdown,
    git: GitCorrelation,
) -> list[str]:
    """Turn a severity breakdown into a plain-English checklist."""

    actions: list[str] = []
    if "code_execution" in severity.signals:
        actions.append(
            "Treat the affected agent session as compromised — kill it "
            "and review its full transcript."
        )
    if git.commit:
        actions.append(f"Revert or audit commit `{git.commit[:12]}` and anything it introduced.")
    if "near_secrets" in severity.signals or "sensitive_path" in severity.signals:
        actions.append(
            "Rotate every credential that lives near the trip site "
            f"({fire.path or (canary.path if canary else 'the fired path')})."
        )
    if "external_url" in severity.signals:
        actions.append(
            "Check egress logs for outbound requests to the URL in the bait; "
            "block it at the firewall if unexpected."
        )
    if canary is not None:
        actions.append(
            f"If this fire is a known false positive, add canary `{canary.id}` to "
            "the ignore list in `canary.toml`."
        )
    actions.append(
        "Re-run `canary check` after remediation to confirm the beacon is quiet."
    )
    if severity.severity in ("high", "critical"):
        actions.insert(0, f"⚠️  Severity **{severity.severity.upper()}** — page the on-call.")
    return actions


# ---------------------------------------------------------------------------
# Git correlation (best-effort, offline-safe)


def _run_git(root: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def correlate_git(root: Path, fire: BeaconRecord) -> GitCorrelation:
    """Best-effort ``git log`` correlation for the fired path.

    Always returns a :class:`GitCorrelation`; ``available=False`` when git
    is missing, the repo isn't a git checkout, or the path is unknown.
    """

    target = fire.path
    if not target:
        return GitCorrelation()

    raw = _run_git(
        root,
        [
            "log",
            "-1",
            "--format=%H%x1f%an%x1f%aI%x1f%s",
            "--",
            target,
        ],
    )
    if not raw:
        return GitCorrelation()

    parts = raw.split("\x1f")
    if len(parts) != 4:
        return GitCorrelation()
    commit, author, committed_at, subject = parts

    files_raw = _run_git(root, ["show", "--name-only", "--format=", commit]) or ""
    touched = [line.strip() for line in files_raw.splitlines() if line.strip()]

    return GitCorrelation(
        commit=commit,
        author=author,
        committed_at=committed_at,
        subject=subject,
        touched_files=touched,
        available=True,
    )


# ---------------------------------------------------------------------------
# Diff


def _read_observed(root: Path, fire: BeaconRecord) -> str:
    if not fire.path:
        return ""
    p = root / fire.path
    if not p.exists() or not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _unified_diff(bait: str, observed: str) -> list[str]:
    import difflib

    return list(
        difflib.unified_diff(
            bait.splitlines(),
            observed.splitlines(),
            fromfile="bait",
            tofile="observed",
            lineterm="",
            n=2,
        )
    )


# ---------------------------------------------------------------------------
# Public API


def build_report(
    root: Path,
    fire: BeaconRecord,
    *,
    severity_weights: dict[str, int] | None = None,
) -> Report:
    """Assemble a :class:`Report` for ``fire`` under ``root``."""

    state = load_state(root)
    canary = next((c for c in state.canaries if c.id == fire.canary_id), None)

    bait_text = canary.marker if canary else ""
    if canary and canary.payload:
        # Reverse canaries stash the actual trigger token in payload; that's
        # what the agent is expected to have emitted verbatim.
        bait_text = canary.payload

    observed_text = _read_observed(root, fire)
    diff = _unified_diff(bait_text, observed_text) if bait_text or observed_text else []
    git = correlate_git(root, fire)
    severity = score_severity(fire, canary, bait_text, observed_text, weights=severity_weights)
    recommendations = recommend_actions(fire, canary, severity, git)

    return Report(
        fire=fire,
        canary=canary,
        bait_text=bait_text,
        observed_text=observed_text,
        diff=diff,
        git=git,
        severity=severity,
        recommendations=recommendations,
    )


# ---------------------------------------------------------------------------
# Renderers — deterministic, no Rich in the output so golden tests are stable.


_SEVERITY_EMOJI: dict[Severity, str] = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🟠",
    "critical": "🔴",
}


def _fmt_iso(dt: datetime | str | None) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def render_markdown(report: Report) -> str:
    """Render ``report`` as GitHub-flavoured markdown."""

    fire = report.fire
    canary = report.canary
    sev = report.severity
    lines: list[str] = []
    lines.append(f"# 🚨 canary fire report: `{fire.canary_id}`")
    lines.append("")
    lines.append(
        f"**Severity:** {_SEVERITY_EMOJI[sev.severity]} `{sev.severity.upper()}` "
        f"(score {sev.score})"
    )
    lines.append("")

    # Metadata
    lines.append("## Canary")
    lines.append("")
    lines.append(f"- **id:** `{fire.canary_id}`")
    lines.append(f"- **type:** `{fire.canary_type}`")
    lines.append(f"- **fired path:** `{fire.path or '—'}`")
    lines.append(f"- **detected via:** `{fire.source}`")
    lines.append(f"- **detected at:** {_fmt_iso(fire.detected_at)}")
    if canary is not None:
        lines.append(f"- **planted at:** {_fmt_iso(canary.planted_at)}")
    lines.append("")

    # Bait vs observed
    lines.append("## Bait vs. observed")
    lines.append("")
    if report.diff:
        lines.append("```diff")
        lines.extend(report.diff)
        lines.append("```")
    else:
        lines.append("_No bait/observed text captured — canary metadata may be missing._")
    lines.append("")

    # Git correlation
    lines.append("## Git correlation")
    lines.append("")
    if report.git.available:
        lines.append(f"- **commit:** `{report.git.commit}`")
        lines.append(f"- **author:** {report.git.author}")
        lines.append(f"- **committed at:** {report.git.committed_at}")
        lines.append(f"- **subject:** {report.git.subject}")
        if report.git.touched_files:
            lines.append("- **touched files:**")
            for f in report.git.touched_files:
                lines.append(f"  - `{f}`")
    else:
        lines.append("_No git history available for this path._")
    lines.append("")

    # Severity breakdown
    lines.append("## Severity signals")
    lines.append("")
    if sev.signals:
        for sig in sev.signals:
            lines.append(f"- `{sig}` (+{sev.weights.get(sig, 0)})")
    else:
        lines.append("- _No weighted signals triggered — scored as `low` by default._")
    lines.append("")

    # Recommendations
    lines.append("## Recommended actions")
    lines.append("")
    for action in report.recommendations:
        lines.append(f"- [ ] {action}")
    lines.append("")
    return "\n".join(lines)


def render_text(report: Report) -> str:
    """Render ``report`` as plain text (no markdown, no colour)."""

    fire = report.fire
    canary = report.canary
    sev = report.severity
    lines: list[str] = []
    lines.append(f"canary fire report: {fire.canary_id}")
    lines.append("=" * (20 + len(fire.canary_id)))
    lines.append(f"severity: {sev.severity.upper()} (score {sev.score})")
    lines.append("")
    lines.append(f"type:         {fire.canary_type}")
    lines.append(f"fired path:   {fire.path or '-'}")
    lines.append(f"detected via: {fire.source}")
    lines.append(f"detected at:  {_fmt_iso(fire.detected_at)}")
    if canary is not None:
        lines.append(f"planted at:   {_fmt_iso(canary.planted_at)}")
    lines.append("")
    lines.append("bait vs. observed:")
    if report.diff:
        lines.extend(f"  {line}" for line in report.diff)
    else:
        lines.append("  (no bait/observed text captured)")
    lines.append("")
    lines.append("git correlation:")
    if report.git.available:
        lines.append(f"  commit:       {report.git.commit}")
        lines.append(f"  author:       {report.git.author}")
        lines.append(f"  committed at: {report.git.committed_at}")
        lines.append(f"  subject:      {report.git.subject}")
        for f in report.git.touched_files:
            lines.append(f"  touched:      {f}")
    else:
        lines.append("  (no git history for this path)")
    lines.append("")
    lines.append("severity signals:")
    if sev.signals:
        for sig in sev.signals:
            lines.append(f"  - {sig} (+{sev.weights.get(sig, 0)})")
    else:
        lines.append("  (no weighted signals triggered)")
    lines.append("")
    lines.append("recommended actions:")
    for action in report.recommendations:
        lines.append(f"  [ ] {action}")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    """Render ``report`` as a stable JSON string for downstream tooling."""

    payload = {
        "schema_version": 1,
        "fire": report.fire.model_dump(mode="json"),
        "canary": report.canary.model_dump(mode="json") if report.canary else None,
        "severity": {
            "level": report.severity.severity,
            "score": report.severity.score,
            "signals": report.severity.signals,
            "weights": report.severity.weights,
        },
        "git": {
            "available": report.git.available,
            "commit": report.git.commit,
            "author": report.git.author,
            "committed_at": report.git.committed_at,
            "subject": report.git.subject,
            "touched_files": report.git.touched_files,
        },
        "diff": report.diff,
        "recommendations": report.recommendations,
    }
    return json.dumps(payload, indent=2, sort_keys=True)
