"""Canary base protocol.

Each canary type knows how to ``plant`` itself across the repo, returning
a list of :class:`~canary_cage.state.PlantedCanary` records, and how to
``uproot`` previously-planted canaries cleanly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import PlantFilter
from ..state import PlantedCanary


class Canary(Protocol):
    """Pluggable canary type."""

    type_name: str

    def plant(
        self, root: Path, plant_filter: PlantFilter | None = None
    ) -> list[PlantedCanary]:
        """Plant canaries under ``root`` and return state records."""

    def uproot(self, root: Path, planted: PlantedCanary) -> None:
        """Reverse a single planted canary."""
