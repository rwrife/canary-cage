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

CanaryType = Literal["markdown"]


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


class CageState(BaseModel):
    """Top-level state document persisted to ``state.json``."""

    schema_version: int = SCHEMA_VERSION
    canaries: list[PlantedCanary] = Field(default_factory=list)


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
