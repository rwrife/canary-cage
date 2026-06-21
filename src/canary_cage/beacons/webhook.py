"""Webhook beacon — POSTs each fire as JSON to a configured URL.

The webhook beacon is intentionally dependency-free: it uses
``urllib.request`` under the hood so the package keeps a tiny install
footprint. Each fire is delivered as a single ``application/json`` POST
with the same payload shape the file/log beacons persist.

Failures are retried with exponential backoff up to ``max_attempts``
times. After the final failure the beacon falls back to appending a
``.canary-cage/webhook.dead`` JSON line so the operator can see what
didn't make it out — beacons must never raise into the scanner.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..state import STATE_DIR_NAME
from .base import BeaconRecord

DEAD_LETTER_FILE = "webhook.dead"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 0.5


def dead_letter_path(root: Path) -> Path:
    return root / STATE_DIR_NAME / DEAD_LETTER_FILE


@dataclass
class WebhookBeacon:
    """POST each :class:`BeaconRecord` as JSON to ``url``."""

    url: str
    timeout: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff: float = DEFAULT_BACKOFF_S
    headers: dict[str, str] = field(default_factory=dict)
    name: str = "webhook"

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("WebhookBeacon requires a non-empty url")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")

    # Seam used by tests to avoid real network calls.
    def _send(self, payload: bytes) -> int:
        req = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "canary-cage-webhook/1",
                **self.headers,
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200))

    def fire(self, root: Path, record: BeaconRecord) -> None:
        payload = json.dumps(
            record.model_dump(mode="json"), sort_keys=True
        ).encode("utf-8")
        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                status = self._send(payload)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - beacons must not raise
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if 200 <= status < 300:
                    return
                last_error = f"HTTP {status}"
            if attempt < self.max_attempts:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
        self._write_dead_letter(root, record, last_error or "unknown error")

    def _write_dead_letter(
        self, root: Path, record: BeaconRecord, error: str
    ) -> None:
        path = dead_letter_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "url": self.url,
                "error": error,
                "record": record.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
