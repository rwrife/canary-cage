"""Tests for the OpenTelemetry beacon (issue #29)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from canary_cage.beacons import BeaconRecord
from canary_cage.beacons.otel import (
    ATTR_COMMIT,
    ATTR_DETAIL,
    ATTR_DETECTED_AT,
    ATTR_ID,
    ATTR_PATH,
    ATTR_REPO,
    ATTR_SOURCE,
    ATTR_TYPE,
    DEFAULT_SERVICE_NAME,
    EVENT_NAME,
    OtelBeacon,
    OtelBeaconMissingDeps,
    dead_letter_path,
)
from canary_cage.canaries import MarkdownCanary
from canary_cage.cli import app
from canary_cage.config import CONFIG_FILE_NAME, CageConfig, OtelConfig, load_config
from canary_cage.scanner import beacons_for, scan
from canary_cage.state import CageState, save_state

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> BeaconRecord:
    base: dict[str, object] = dict(
        canary_id="md-abc",
        canary_type="markdown",
        source="working-tree",
        detail="sentinel missing from README.md",
        path="README.md",
        detected_at=datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return BeaconRecord(**base)  # type: ignore[arg-type]


def _in_memory_exporter():
    """Return a fresh in-memory span exporter for assertion in tests."""
    pytest.importorskip("opentelemetry")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    return InMemorySpanExporter()


# ---------------------------------------------------------------------------
# Fire happy paths
# ---------------------------------------------------------------------------


def test_fire_records_span_with_expected_attributes(tmp_path: Path) -> None:
    exporter = _in_memory_exporter()
    beacon = OtelBeacon.for_testing(exporter)
    try:
        beacon.fire(tmp_path, _record())
    finally:
        beacon.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == EVENT_NAME
    assert span.attributes[ATTR_ID] == "md-abc"
    assert span.attributes[ATTR_TYPE] == "markdown"
    assert span.attributes[ATTR_SOURCE] == "working-tree"
    assert span.attributes[ATTR_DETAIL] == "sentinel missing from README.md"
    assert span.attributes[ATTR_PATH] == "README.md"
    assert span.attributes[ATTR_DETECTED_AT] == "2026-07-02T12:00:00+00:00"
    # `canary.fired = true` marker for query-only stacks.
    assert span.attributes["canary.fired"] is True

    events = list(span.events)
    assert len(events) == 1
    assert events[0].name == EVENT_NAME
    assert events[0].attributes[ATTR_ID] == "md-abc"


def test_fire_omits_optional_path_attribute_when_missing(tmp_path: Path) -> None:
    exporter = _in_memory_exporter()
    beacon = OtelBeacon.for_testing(exporter)
    try:
        beacon.fire(tmp_path, _record(path=None))
    finally:
        beacon.shutdown()

    span = exporter.get_finished_spans()[0]
    assert ATTR_PATH not in span.attributes


def test_service_name_propagates_to_resource(tmp_path: Path) -> None:
    exporter = _in_memory_exporter()
    beacon = OtelBeacon.for_testing(
        exporter,
        service_name="canary-cage-prod",
        resource_attributes={"environment": "production"},
    )
    try:
        beacon.fire(tmp_path, _record())
    finally:
        beacon.shutdown()

    span = exporter.get_finished_spans()[0]
    resource_attrs = dict(span.resource.attributes)
    assert resource_attrs["service.name"] == "canary-cage-prod"
    assert resource_attrs["environment"] == "production"


def test_fire_populates_repo_and_commit_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fake a git repo without invoking real git commands.
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CANARY_CAGE_TEST_HEAD_SHA", "abc123")

    from canary_cage.beacons import otel as otel_mod

    monkeypatch.setattr(otel_mod, "_git_repo_name", lambda _: "rwrife/canary-cage")

    exporter = _in_memory_exporter()
    beacon = OtelBeacon.for_testing(exporter)
    try:
        beacon.fire(tmp_path, _record())
    finally:
        beacon.shutdown()

    span = exporter.get_finished_spans()[0]
    assert span.attributes[ATTR_REPO] == "rwrife/canary-cage"
    assert span.attributes[ATTR_COMMIT] == "abc123"


def test_disabled_beacon_is_a_no_op(tmp_path: Path) -> None:
    exporter = _in_memory_exporter()
    beacon = OtelBeacon.for_testing(exporter)
    beacon.enabled = False
    try:
        beacon.fire(tmp_path, _record())
    finally:
        beacon.shutdown()
    assert exporter.get_finished_spans() == ()


def test_beacon_is_inert_after_shutdown(tmp_path: Path) -> None:
    exporter = _in_memory_exporter()
    beacon = OtelBeacon.for_testing(exporter)
    beacon.fire(tmp_path, _record())
    beacon.shutdown()
    # Fires after shutdown are silent no-ops, not re-wired batch
    # processors leaking to a real OTLP endpoint.
    beacon.fire(tmp_path, _record(canary_id="md-second"))
    ids = [s.attributes[ATTR_ID] for s in exporter.get_finished_spans()]
    assert ids == ["md-abc"]


# ---------------------------------------------------------------------------
# Missing-deps behaviour
# ---------------------------------------------------------------------------


def test_missing_deps_produces_clean_dead_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canary_cage.beacons import otel as otel_mod

    def _boom() -> None:
        raise OtelBeaconMissingDeps("SDK not installed")

    monkeypatch.setattr(otel_mod, "_import_sdk", _boom)

    beacon = OtelBeacon(enabled=True)
    beacon.fire(tmp_path, _record())  # must not raise

    dl = dead_letter_path(tmp_path)
    assert dl.exists()
    line = json.loads(dl.read_text(encoding="utf-8").splitlines()[0])
    assert "SDK not installed" in line["error"]
    assert line["record"]["canary_id"] == "md-abc"


def test_install_hint_mentions_extras() -> None:
    from canary_cage.beacons.otel import _install_hint

    hint = _install_hint()
    assert "canary-cage[otel]" in hint
    assert "pip install" in hint


def test_beacon_swallows_export_errors(tmp_path: Path) -> None:
    class ExplodingExporter:
        def export(self, spans):  # noqa: ANN001, ARG002
            raise RuntimeError("collector down")

        def shutdown(self) -> None:  # noqa: D401 - noop
            return

        def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
            return True

    beacon = OtelBeacon.for_testing(ExplodingExporter())  # type: ignore[arg-type]
    try:
        beacon.fire(tmp_path, _record())  # must not raise
        # Repeat call must also not raise: proves the beacon is idempotent
        # in the face of collector failures.
        beacon.fire(tmp_path, _record())
    finally:
        beacon.shutdown()


# ---------------------------------------------------------------------------
# Import-guarded module (no OTel installed)
# ---------------------------------------------------------------------------


def test_import_sdk_raises_missing_deps_when_no_otel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if opentelemetry vanishes at runtime, we get a clean error."""
    # Nuke every already-imported opentelemetry.* module so the fresh
    # import inside _import_sdk actually fails.
    for mod_name in list(sys.modules):
        if mod_name == "opentelemetry" or mod_name.startswith("opentelemetry."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    real_import = __import__

    def fake_import(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    from canary_cage.beacons.otel import _import_sdk

    with pytest.raises(OtelBeaconMissingDeps) as excinfo:
        _import_sdk()
    assert "canary-cage[otel]" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


def test_config_loads_otel_table(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\ntypes = ["markdown"]\n\n'
        '[beacons.otel]\nenabled = true\nservice_name = "cage-prod"\n'
        'resource_attributes = { environment = "production" }\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.otel.enabled is True
    assert cfg.otel.service_name == "cage-prod"
    assert cfg.otel.resource_attributes == {"environment": "production"}


def test_config_defaults_are_disabled() -> None:
    cfg = CageConfig()
    assert isinstance(cfg.otel, OtelConfig)
    assert cfg.otel.enabled is False
    assert cfg.otel.service_name == DEFAULT_SERVICE_NAME
    assert cfg.otel.resource_attributes == {}


def test_config_rejects_empty_service_name(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.otel]\nenabled = true\nservice_name = "   "\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_beacons_for_includes_otel_when_enabled(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.otel]\nenabled = true\n', encoding="utf-8"
    )
    sinks = beacons_for(tmp_path)
    assert any(s.name == "otel" for s in sinks)


def test_beacons_for_omits_otel_when_disabled(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.otel]\nenabled = false\n', encoding="utf-8"
    )
    sinks = beacons_for(tmp_path)
    assert all(s.name != "otel" for s in sinks)


def test_beacons_for_no_otel_by_default(tmp_path: Path) -> None:
    sinks = beacons_for(tmp_path)
    assert all(s.name != "otel" for s in sinks)


def test_cli_init_mentions_otel(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 0
    body = (tmp_path / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    assert "[beacons.otel]" in body
    assert "canary-cage[otel]" in body


# ---------------------------------------------------------------------------
# End-to-end: scanner wires OtelBeacon and every fire produces a span
# ---------------------------------------------------------------------------


def test_scan_emits_otel_span_per_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[beacons.otel]\nenabled = true\n', encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    planted = MarkdownCanary().plant(tmp_path)
    save_state(tmp_path, CageState(canaries=planted))
    (tmp_path / "README.md").write_text("# wiped\n", encoding="utf-8")

    exporter = _in_memory_exporter()

    # Swap OtelBeacon.fire → route through a for-testing beacon backed
    # by the in-memory exporter so we can assert on emitted spans.
    from canary_cage.beacons import otel as otel_mod

    test_beacon = OtelBeacon.for_testing(exporter)
    monkeypatch.setattr(
        otel_mod.OtelBeacon, "_ensure_tracer", lambda self: test_beacon._tracer
    )

    try:
        fires = scan(tmp_path)
    finally:
        test_beacon.shutdown()

    assert len(fires) == 1
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes[ATTR_ID] == planted[0].id
    assert spans[0].attributes[ATTR_TYPE] == "markdown"
