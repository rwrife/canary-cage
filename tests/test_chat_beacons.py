"""Tests for the Slack / Discord chat beacons."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from canary_cage.beacons import BeaconRecord, DiscordBeacon, SlackBeacon
from canary_cage.beacons.chat import ChatBeacon, dead_letter_path
from canary_cage.canaries import MarkdownCanary
from canary_cage.config import CONFIG_FILE_NAME, load_config
from canary_cage.scanner import beacons_for, scan
from canary_cage.state import CageState, save_state


def _record() -> BeaconRecord:
    return BeaconRecord(
        canary_id="md-abc",
        canary_type="markdown",
        source="working-tree",
        detail="sentinel missing from README.md",
        path="README.md",
    )


class _FakeChat(ChatBeacon):
    """ChatBeacon with ``_send`` scripted for tests."""

    def __init__(self, responses: list[object], flavor: str = "slack", **kw: object) -> None:
        super().__init__(
            url="https://example.invalid/hook",
            flavor=flavor,  # type: ignore[arg-type]
            backoff=0.0,
            **kw,
        )
        self._responses = list(responses)
        self.calls: list[bytes] = []

    def _send(self, payload: bytes) -> int:  # type: ignore[override]
        self.calls.append(payload)
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return int(nxt)  # type: ignore[arg-type]


def test_slack_payload_uses_text_key(tmp_path: Path) -> None:
    b = _FakeChat([200], flavor="slack")
    b.fire(tmp_path, _record())
    body = json.loads(b.calls[0])
    assert "text" in body and "content" not in body
    assert "md-abc" in body["text"]
    assert "markdown" in body["text"]
    assert "README.md" in body["text"]


def test_discord_payload_uses_content_key(tmp_path: Path) -> None:
    b = _FakeChat([204], flavor="discord")
    b.fire(tmp_path, _record())
    body = json.loads(b.calls[0])
    assert "content" in body and "text" not in body
    assert "md-abc" in body["content"]


def test_chat_includes_file_snippet(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hello\nworld\n", encoding="utf-8")
    b = _FakeChat([200])
    b.fire(tmp_path, _record())
    body = json.loads(b.calls[0])
    assert "# hello" in body["text"]
    assert "```" in body["text"]


def test_chat_snippet_truncates(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x" * 5000, encoding="utf-8")
    b = _FakeChat([200], snippet_chars=64)
    b.fire(tmp_path, _record())
    body = json.loads(b.calls[0])
    # Truncation marker present, and the rendered message stays well
    # short of the raw 5000 chars.
    assert "…" in body["text"]
    assert len(body["text"]) < 400


def test_chat_retries_then_succeeds(tmp_path: Path) -> None:
    b = _FakeChat([urllib.error.URLError("nope"), 500, 200])
    b.fire(tmp_path, _record())
    assert len(b.calls) == 3
    assert not dead_letter_path(tmp_path).exists()


def test_chat_dead_letters_after_exhaustion(tmp_path: Path) -> None:
    b = _FakeChat([500, 500, 500])
    b.fire(tmp_path, _record())
    dl = dead_letter_path(tmp_path)
    assert dl.exists()
    line = json.loads(dl.read_text(encoding="utf-8").splitlines()[0])
    assert line["flavor"] == "slack"
    assert line["error"] == "HTTP 500"
    assert line["record"]["canary_id"] == "md-abc"


def test_chat_swallows_unexpected_exceptions(tmp_path: Path) -> None:
    b = _FakeChat([RuntimeError("weird"), RuntimeError("weird"), RuntimeError("weird")])
    b.fire(tmp_path, _record())  # must not raise
    assert dead_letter_path(tmp_path).exists()


def test_chat_requires_url() -> None:
    with pytest.raises(ValueError):
        SlackBeacon(url="")
    with pytest.raises(ValueError):
        DiscordBeacon(url="")


def test_chat_rejects_unknown_flavor() -> None:
    with pytest.raises(ValueError):
        ChatBeacon(url="https://x", flavor="teams")  # type: ignore[arg-type]


def test_chat_beacon_names() -> None:
    assert SlackBeacon(url="https://x").name == "slack"
    assert DiscordBeacon(url="https://x").name == "discord"


def test_config_loads_slack_and_discord(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\ntypes = ["markdown"]\n\n'
        '[beacons.slack]\nurl = "https://hooks.slack.com/services/T/B/X"\n'
        'snippet_chars = 100\n\n'
        '[beacons.discord]\nurl = "https://discord.com/api/webhooks/1/x"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.slack.url == "https://hooks.slack.com/services/T/B/X"
    assert cfg.slack.snippet_chars == 100
    assert cfg.discord.url == "https://discord.com/api/webhooks/1/x"


def test_beacons_for_includes_slack_and_discord(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.slack]\nurl = "https://hooks.slack.com/services/T/B/X"\n\n'
        '[beacons.discord]\nurl = "https://discord.com/api/webhooks/1/x"\n',
        encoding="utf-8",
    )
    names = [s.name for s in beacons_for(tmp_path)]
    assert "slack" in names
    assert "discord" in names


def test_scan_fires_slack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.slack]\nurl = "https://hooks.slack.com/services/T/B/X"\n',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    planted = MarkdownCanary().plant(tmp_path)
    save_state(tmp_path, CageState(canaries=planted))
    (tmp_path / "README.md").write_text("# wiped\n", encoding="utf-8")

    sent: list[bytes] = []

    def fake_send(self: SlackBeacon, payload: bytes) -> int:  # noqa: ARG001
        sent.append(payload)
        return 200

    monkeypatch.setattr(SlackBeacon, "_send", fake_send)
    fires = scan(tmp_path)
    assert len(fires) == 1
    assert len(sent) == 1
    body = json.loads(sent[0])
    assert planted[0].id in body["text"]


def test_cli_init_mentions_chat_beacons(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from canary_cage.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 0
    body = (tmp_path / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    assert "[beacons.slack]" in body
    assert "[beacons.discord]" in body
