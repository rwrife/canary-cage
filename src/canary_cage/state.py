"""State models + JSON persistence for canary-cage.

State lives at ``.canary-cage/state.json`` inside the target repo. The
schema is intentionally tiny and append-only friendly so new canary types
can land in later milestones without breaking older state files.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

STATE_DIR_NAME = ".canary-cage"
STATE_FILE_NAME = "state.json"
SCHEMA_VERSION = 1

CanaryType = Literal["markdown", "docstring", "todo", "manifest"]
HoneyKind = Literal["issue", "pr"]


class PlantedCanary(BaseModel):
    """A single planted canary.

    ``path`` is stored relative to the cage root (the directory that
    contains ``.canary-cage/``) so state files stay portable.
    """

    id: str
    type: CanaryType
    path: str
    marker: str
    planted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Time-bomb: when set, the canary is dormant until ``armed_at`` has
    # passed. ``None`` means "always armed" — the historical behaviour.
    armed_at: datetime | None = None

    def is_armed(self, now: datetime | None = None) -> bool:
        """Return True if the canary is currently armed (live)."""
        if self.armed_at is None:
            return True
        current = now if now is not None else datetime.now(UTC)
        # Coerce naive datetimes to UTC so comparisons never explode.
        armed = self.armed_at
        if armed.tzinfo is None:
            armed = armed.replace(tzinfo=UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current >= armed


class HoneyArtifact(BaseModel):
    """A canary-bearing GitHub issue or PR planted via ``gh``.

    ``github_id`` is the issue/PR number (they share a namespace on
    GitHub). ``body_snapshot`` is what we last saw on the server so
    :func:`canary_cage.honey.check_honey_fires` can spot mutations.
    ``last_comment_id`` tracks the newest comment id we've already
    observed — anything newer counts as a fire.
    """

    id: str
    kind: HoneyKind
    repo: str  # "owner/name"
    github_id: int
    url: str
    marker: str
    body_snapshot: str
    branch: str | None = None  # only for kind=="pr"
    last_comment_id: int = 0
    planted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CageState(BaseModel):
    """Top-level state document persisted to ``state.json``."""

    schema_version: int = SCHEMA_VERSION
    canaries: list[PlantedCanary] = Field(default_factory=list)
    honey: list[HoneyArtifact] = Field(default_factory=list)


def state_dir(root: Path) -> Path:
    return root / STATE_DIR_NAME


def state_path(root: Path) -> Path:
    return state_dir(root) / STATE_FILE_NAME


def load_state(root: Path) -> CageState:
    """Load state from ``root/.canary-cage/state.json``.

    Returns an empty :class:`CageState` if the file does not exist yet.
    """

    path = state_path(root)
    if not path.exists():
        return CageState()
    raw = path.read_text(encoding="utf-8")
    return CageState.model_validate_json(raw)


def save_state(root: Path, state: CageState) -> Path:
    """Persist ``state`` to disk, creating the state dir if needed."""

    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
