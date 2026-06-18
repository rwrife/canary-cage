"""File beacon — writes one JSON file per fire to ``.canary-cage/fired/``."""

from __future__ import annotations

import json
from pathlib import Path

from ..state import STATE_DIR_NAME
from .base import BeaconRecord

FIRED_DIR_NAME = "fired"


def fired_dir(root: Path) -> Path:
    return root / STATE_DIR_NAME / FIRED_DIR_NAME


class FileBeacon:
    """Persist each fire as ``.canary-cage/fired/<canary_id>.json``."""

    name = "file"

    def fire(self, root: Path, record: BeaconRecord) -> None:
        target_dir = fired_dir(root)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{record.canary_id}.json"
        target.write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
