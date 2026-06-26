"""Beacon protocol + shared record type.

A *beacon* is a sink that gets called when the scanner decides a canary
fired. Beacons are intentionally cheap & local-by-default — file writes
and log appends. Webhook / Slack adapters land in later milestones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

FireSource = Literal["working-tree", "git-history", "stray-file", "lockfile"]


class BeaconRecord(BaseModel):
    """One firing event."""

    canary_id: str
    canary_type: str
    source: FireSource
    detail: str
    path: str | None = None
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Beacon(Protocol):
    """Pluggable beacon sink."""

    name: str

    def fire(self, root: Path, record: BeaconRecord) -> None: ...
