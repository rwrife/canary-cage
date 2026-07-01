"""Agent fingerprinter — guess *who* tripped a canary.

The fingerprinter consumes a :class:`FireContext` (planted canary +
observed mutation + surrounding git/commit info + optional webhook
metadata) and returns ranked :class:`AgentCandidate`\\s with a confidence
score in ``[0, 1]`` and the list of matched signals.

Rules live in a JSON pack so they can be extended without touching
package code. The default pack ships at
``src/canary_cage/fingerprints/rules.json``; operators can drop a
``fingerprints.json`` at the cage root to add or override agents.

The output schema is intentionally flat / additive so it can be merged
into existing diff and check JSON without breaking consumers:

    {
      "attributed_to": {
        "top": {"agent": "...", "confidence": 0.83},
        "candidates": [
          {"agent": "...", "confidence": 0.83, "signals": ["rule.id", ...]}
        ]
      }
    }
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .state import PlantedCanary, load_state

_RULES_PACKAGE = "canary_cage.fingerprints"
_RULES_FILENAME = "rules.json"
_OVERRIDE_FILENAME = "fingerprints.json"

# Fields a rule can be evaluated against.
_RULE_FIELDS = (
    "commit_message",
    "commit_author",
    "content",
    "repo_paths",
    "user_agent",
)


@dataclass
class FireContext:
    """Everything we know about one fire, fed to the fingerprinter."""

    canary_id: str
    canary_type: str
    path: str | None = None
    planted_content: str | None = None
    observed_content: str | None = None
    commit_message: str | None = None
    commit_author: str | None = None
    commit_sha: str | None = None
    user_agent: str | None = None
    repo_paths: list[str] = field(default_factory=list)

    def field_text(self, name: str) -> str:
        if name == "commit_message":
            return self.commit_message or ""
        if name == "commit_author":
            return self.commit_author or ""
        if name == "user_agent":
            return self.user_agent or ""
        if name == "repo_paths":
            return "\n".join(self.repo_paths)
        if name == "content":
            # The "content" field is the union of planted + observed so
            # rules can fire on either the bait or what replaced it.
            return "\n".join(
                t for t in (self.planted_content, self.observed_content) if t
            )
        return ""


@dataclass
class AgentCandidate:
    agent: str
    display: str
    confidence: float
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "display": self.display,
            "confidence": round(self.confidence, 3),
            "signals": list(self.signals),
        }


@dataclass
class FingerprintReport:
    """Full output of :meth:`Fingerprinter.identify`."""

    candidates: list[AgentCandidate]

    @property
    def top(self) -> AgentCandidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, object]:
        if not self.candidates:
            return {"top": None, "candidates": []}
        return {
            "top": {
                "agent": self.candidates[0].agent,
                "confidence": round(self.candidates[0].confidence, 3),
            },
            "candidates": [c.to_dict() for c in self.candidates],
        }


def _load_rules_pack(root: Path | None = None) -> dict:
    """Load the bundled rule pack, optionally merged with a repo override."""
    base_text = (
        resources.files(_RULES_PACKAGE).joinpath(_RULES_FILENAME).read_text(encoding="utf-8")
    )
    base = json.loads(base_text)
    if root is not None:
        override_path = root / _OVERRIDE_FILENAME
        if override_path.exists():
            try:
                override = json.loads(override_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return base
            base = _merge_packs(base, override)
    return base


def _merge_packs(base: dict, override: dict) -> dict:
    """Merge ``override`` into ``base`` by agent name (override wins)."""
    out = {"version": override.get("version", base.get("version", 1)), "agents": []}
    by_name: dict[str, dict] = {a["agent"]: dict(a) for a in base.get("agents", [])}
    for agent in override.get("agents", []):
        by_name[agent["agent"]] = dict(agent)
    out["agents"] = list(by_name.values())
    return out


class Fingerprinter:
    """Match a :class:`FireContext` against an agent rule pack."""

    def __init__(self, root: Path | None = None, pack: dict | None = None) -> None:
        self.root = root
        self.pack = pack if pack is not None else _load_rules_pack(root)
        # Pre-compile patterns once.
        self._compiled: dict[str, list[tuple[str, float, str, re.Pattern[str]]]] = {}
        for agent in self.pack.get("agents", []):
            compiled: list[tuple[str, float, str, re.Pattern[str]]] = []
            for rule in agent.get("rules", []):
                field_name = rule.get("field", "content")
                if field_name not in _RULE_FIELDS:
                    continue
                try:
                    pat = re.compile(rule["pattern"])
                except (re.error, KeyError):
                    continue
                compiled.append(
                    (
                        rule.get("id", "<unnamed>"),
                        float(rule.get("weight", 0.1)),
                        field_name,
                        pat,
                    )
                )
            self._compiled[agent["agent"]] = compiled

    def identify(self, ctx: FireContext) -> FingerprintReport:
        """Return ranked candidates for ``ctx``."""
        candidates: list[AgentCandidate] = []
        for agent in self.pack.get("agents", []):
            name = agent["agent"]
            rules = self._compiled.get(name, [])
            if not rules:
                continue
            matched_signals: list[str] = []
            score = 0.0
            for rule_id, weight, field_name, pat in rules:
                text = ctx.field_text(field_name)
                if not text:
                    continue
                if pat.search(text):
                    matched_signals.append(rule_id)
                    # Cap any one rule's contribution so a single regex
                    # can't pin confidence to 1.0 on a weak match.
                    score += min(weight, 0.5)
            if not matched_signals:
                continue
            confidence = _squash(score)
            candidates.append(
                AgentCandidate(
                    agent=name,
                    display=agent.get("display", name),
                    confidence=confidence,
                    signals=matched_signals,
                )
            )
        candidates.sort(key=lambda c: (-c.confidence, c.agent))
        return FingerprintReport(candidates=candidates)


def _squash(score: float) -> float:
    """Map a raw additive score into a (0, 1) confidence."""
    if score <= 0:
        return 0.0
    # Simple saturating curve: 1 - exp(-score). One weight=0.45 hit ≈ 0.36,
    # two hits ≈ 0.59, three ≈ 0.74. Good enough for ranked guesses.
    import math

    return round(1.0 - math.exp(-score), 3)


def context_from_canary(
    root: Path,
    canary: PlantedCanary,
    *,
    user_agent: str | None = None,
) -> FireContext:
    """Build a :class:`FireContext` from a planted canary + repo state."""
    observed: str | None = None
    target = root / canary.path
    if target.exists():
        try:
            observed = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            observed = None

    commit_sha, commit_msg, commit_author = _last_commit_touching(root, canary.path)
    repo_paths = _git_tracked_paths(root)

    return FireContext(
        canary_id=canary.id,
        canary_type=canary.type,
        path=canary.path,
        observed_content=observed,
        commit_message=commit_msg,
        commit_author=commit_author,
        commit_sha=commit_sha,
        user_agent=user_agent,
        repo_paths=repo_paths,
    )


def _last_commit_touching(
    root: Path, path: str
) -> tuple[str | None, str | None, str | None]:
    """Return (sha, full message, ``Name <email>``) for the last commit
    that touched ``path``, or (None, None, None)."""
    if not (root / ".git").exists():
        return None, None, None
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "-n",
                "1",
                "--format=%H%x00%an <%ae>%x00%B",
                "--",
                path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None, None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, None, None
    parts = proc.stdout.split("\x00", 2)
    if len(parts) != 3:
        return None, None, None
    sha, author, msg = parts
    return sha.strip() or None, msg.strip() or None, author.strip() or None


def _git_tracked_paths(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.splitlines() if p]


def identify_for_canary_id(
    root: Path, canary_id: str, *, user_agent: str | None = None
) -> tuple[FireContext, FingerprintReport] | None:
    """Resolve a canary id to ``(context, report)`` or return ``None``."""
    state = load_state(root)
    canary = next((c for c in state.canaries if c.id == canary_id), None)
    if canary is None:
        return None
    ctx = context_from_canary(root, canary, user_agent=user_agent)
    report = Fingerprinter(root=root).identify(ctx)
    return ctx, report


__all__ = [
    "AgentCandidate",
    "FireContext",
    "FingerprintReport",
    "Fingerprinter",
    "context_from_canary",
    "identify_for_canary_id",
]
