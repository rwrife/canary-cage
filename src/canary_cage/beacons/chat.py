"""Slack / Discord chat beacons.

Pings a chat channel the second the scanner thinks a canary fired.
Both Slack and Discord expose dead-simple "incoming webhook" URLs that
take a JSON POST, so this module is a thin formatter on top of the same
retry + dead-letter plumbing the raw :class:`WebhookBeacon` already
uses.

Each fire is rendered as a short, human-readable message:

```
🚨 canary-cage: md-abcdef fired (markdown, working-tree)
   sentinel missing from README.md
   ```
   # README
   ...first ~240 chars of the affected file...
   ```
```

If the planted file can be read, a short context snippet is attached so
the on-call human can eyeball what the agent touched without leaving
Slack/Discord. Beacons must never raise — network failures, bad URLs,
and odd responses fall back to a JSON dead-letter line on disk.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from ..state import STATE_DIR_NAME
from .base import BeaconRecord

ChatFlavor = Literal["slack", "discord"]

DEAD_LETTER_FILE = "chat.dead"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 0.5
DEFAULT_SNIPPET_CHARS = 240


def dead_letter_path(root: Path) -> Path:
    return root / STATE_DIR_NAME / DEAD_LETTER_FILE


def _read_snippet(root: Path, rel: str | None, max_chars: int) -> str:
    """Return a short, single-block snippet of the affected file, or ``""``."""
    if not rel or max_chars <= 0:
        return ""
    target = root / rel
    try:
        text = target.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return ""
    text = text.strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[: max_chars].rstrip() + "…"
    return text


@dataclass
class ChatBeacon:
    """Base class for Slack/Discord webhook beacons.

    Subclasses set :attr:`flavor` and :attr:`name`. The class is usable
    directly too (e.g. for tests) by passing ``flavor=`` explicitly.
    """

    url: str
    flavor: ChatFlavor = "slack"
    timeout: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff: float = DEFAULT_BACKOFF_S
    headers: dict[str, str] = field(default_factory=dict)
    snippet_chars: int = DEFAULT_SNIPPET_CHARS
    name: str = "chat"

    _VALID_FLAVORS: ClassVar[tuple[str, ...]] = ("slack", "discord")

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError(f"{type(self).__name__} requires a non-empty url")
        if self.flavor not in self._VALID_FLAVORS:
            raise ValueError(
                f"unknown chat flavor {self.flavor!r} "
                f"(known: {', '.join(self._VALID_FLAVORS)})"
            )
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")
        if self.snippet_chars < 0:
            raise ValueError("snippet_chars must be >= 0")

    # ------------------------------------------------------------------
    # Message rendering
    # ------------------------------------------------------------------
    def _summary(self, record: BeaconRecord) -> str:
        return (
            f"🚨 canary-cage: `{record.canary_id}` fired "
            f"({record.canary_type}, {record.source})"
        )

    def _body_lines(self, record: BeaconRecord, snippet: str) -> list[str]:
        lines = [record.detail]
        if record.path:
            lines.append(f"file: `{record.path}`")
        if snippet:
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
        return lines

    def _slack_payload(self, record: BeaconRecord, snippet: str) -> dict[str, object]:
        text = "\n".join([self._summary(record), *self._body_lines(record, snippet)])
        return {"text": text}

    def _discord_payload(self, record: BeaconRecord, snippet: str) -> dict[str, object]:
        # Discord caps content at 2000 chars; the rendered message is
        # already tiny but we trim defensively just in case a future
        # caller jacks the snippet size up.
        text = "\n".join([self._summary(record), *self._body_lines(record, snippet)])
        if len(text) > 1900:
            text = text[:1900] + "…"
        return {"content": text}

    def _payload(self, record: BeaconRecord, snippet: str) -> dict[str, object]:
        if self.flavor == "discord":
            return self._discord_payload(record, snippet)
        return self._slack_payload(record, snippet)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------
    # Seam used by tests to avoid real network calls.
    def _send(self, payload: bytes) -> int:
        req = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"canary-cage-{self.flavor}/1",
                **self.headers,
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200))

    def fire(self, root: Path, record: BeaconRecord) -> None:
        snippet = _read_snippet(root, record.path, self.snippet_chars)
        payload = json.dumps(self._payload(record, snippet), sort_keys=True).encode("utf-8")
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
                "flavor": self.flavor,
                "url": self.url,
                "error": error,
                "record": record.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


@dataclass
class SlackBeacon(ChatBeacon):
    """Slack incoming-webhook beacon."""

    flavor: ChatFlavor = "slack"
    name: str = "slack"


@dataclass
class DiscordBeacon(ChatBeacon):
    """Discord incoming-webhook beacon."""

    flavor: ChatFlavor = "discord"
    name: str = "discord"
