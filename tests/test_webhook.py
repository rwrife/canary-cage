"""Tests for the webhook beacon (M6)."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from typer.testing import CliRunner

from canary_cage.beacons import BeaconRecord, WebhookBeacon
from canary_cage.beacons.webhook import dead_letter_path
from canary_cage.canaries import MarkdownCanary
from canary_cage.cli import app
from canary_cage.config import CONFIG_FILE_NAME, load_config
from canary_cage.scanner import beacons_for, scan
from canary_cage.state import CageState, save_state

runner = CliRunner()


def _record() -> BeaconRecord:
    return BeaconRecord(
        canary_id="md-abc",
        canary_type="markdown",
        source="working-tree",
        detail="sentinel missing",
        path="README.md",
    )


class _FakeBeacon(WebhookBeacon):
    """WebhookBeacon with ``_send`` swapped out for a scripted response."""

    def __init__(self, responses: list[object], **kw: object) -> None:
        super().__init__(url="https://example.invalid/hook", backoff=0.0, **kw)
        self._responses = list(responses)
        self.calls: list[bytes] = []

    def _send(self, payload: bytes) -> int:  # type: ignore[override]
        self.calls.append(payload)
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return int(nxt)  # type: ignore[arg-type]


def test_webhook_success_sends_once(tmp_path: Path) -> None:
    b = _FakeBeacon([200])
    b.fire(tmp_path, _record())
    assert len(b.calls) == 1
    payload = json.loads(b.calls[0])
    assert payload["canary_id"] == "md-abc"
    assert not dead_letter_path(tmp_path).exists()


def test_webhook_retries_then_succeeds(tmp_path: Path) -> None:
    b = _FakeBeacon([urllib.error.URLError("boom"), 500, 204])
    b.fire(tmp_path, _record())
    assert len(b.calls) == 3
    assert not dead_letter_path(tmp_path).exists()


def test_webhook_dead_letters_after_exhaustion(tmp_path: Path) -> None:
    b = _FakeBeacon([500, 500, 500])
    b.fire(tmp_path, _record())
    assert len(b.calls) == 3
    dl = dead_letter_path(tmp_path)
    assert dl.exists()
    line = json.loads(dl.read_text(encoding="utf-8").splitlines()[0])
    assert line["error"] == "HTTP 500"
    assert line["record"]["canary_id"] == "md-abc"


def test_webhook_swallows_unexpected_exceptions(tmp_path: Path) -> None:
    b = _FakeBeacon([RuntimeError("weird"), RuntimeError("weird"), RuntimeError("weird")])
    b.fire(tmp_path, _record())  # must not raise
    assert dead_letter_path(tmp_path).exists()


def test_webhook_requires_url() -> None:
    with pytest.raises(ValueError):
        WebhookBeacon(url="")


def test_config_loads_webhook_table(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\ntypes = ["markdown"]\n\n'
        '[beacons.webhook]\nurl = "https://example.com/h"\ntimeout = 2.5\n'
        'max_attempts = 4\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.webhook.url == "https://example.com/h"
    assert cfg.webhook.timeout == 2.5
    assert cfg.webhook.max_attempts == 4


def test_beacons_for_includes_webhook_when_configured(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.webhook]\nurl = "https://example.com/h"\n', encoding="utf-8"
    )
    sinks = beacons_for(tmp_path)
    names = [s.name for s in sinks]
    assert "webhook" in names


def test_beacons_for_no_webhook_by_default(tmp_path: Path) -> None:
    sinks = beacons_for(tmp_path)
    assert [s.name for s in sinks] == ["file", "log"]


def test_scan_fires_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.webhook]\nurl = "https://example.com/h"\n', encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    planted = MarkdownCanary().plant(tmp_path)
    save_state(tmp_path, CageState(canaries=planted))
    (tmp_path / "README.md").write_text("# wiped\n", encoding="utf-8")

    sent: list[bytes] = []

    def fake_send(self: WebhookBeacon, payload: bytes) -> int:  # noqa: ARG001
        sent.append(payload)
        return 200

    monkeypatch.setattr(WebhookBeacon, "_send", fake_send)
    fires = scan(tmp_path)
    assert len(fires) == 1
    assert len(sent) == 1
    body = json.loads(sent[0])
    assert body["canary_id"] == planted[0].id


def test_cli_init_mentions_webhook(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 0
    body = (tmp_path / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    assert "[beacons.webhook]" in body
