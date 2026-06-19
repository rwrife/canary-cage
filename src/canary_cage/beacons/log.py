"""Log beacon — appends one JSON line per fire to ``.canary-cage/beacon.log``."""

from __future__ import annotations

import json
from pathlib import Path

from ..state import STATE_DIR_NAME
from .base import BeaconRecord

LOG_FILE_NAME = "beacon.log"


def log_path(root: Path) -> Path:
    return root / STATE_DIR_NAME / LOG_FILE_NAME


class LogBeacon:
    """Append a JSON line per fire to a single log file."""

    name = "log"

    def fire(self, root: Path, record: BeaconRecord) -> None:
        path = log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.model_dump(mode="json"), sort_keys=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
