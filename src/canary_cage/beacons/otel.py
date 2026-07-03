"""OpenTelemetry beacon — export canary fires as OTel spans/events.

Teams running canary-cage at scale already have an observability stack
(Honeycomb, Datadog, Grafana Tempo, Jaeger, ...). Instead of asking them
to bolt on a bespoke pipeline for our JSON dead-letter or Slack pings,
this beacon speaks the standard: every fire becomes a short-lived span
carrying a single ``canary.fire`` event, with the well-known set of
canary attributes and the source-of-truth timestamp.

Wire format & config
--------------------
The beacon defers all endpoint / protocol / auth choices to the
`OpenTelemetry SDK env-var contract
<https://opentelemetry.io/docs/specs/otel/protocol/exporter/>`_
(``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_HEADERS``,
``OTEL_EXPORTER_OTLP_PROTOCOL``, ...). ``[beacons.otel]`` only owns the
knobs that don't have a natural env-var: ``enabled`` (bool),
``service_name`` (str), plus a small ``resource_attributes`` map for
extra ``Resource`` labels (region, environment, ...).

Optional dep
------------
The OTel SDK is a chunky install, so we keep it behind a ``[otel]``
extra: ``pip install canary-cage[otel]``. The beacon reports a clean,
actionable :class:`OtelBeaconMissingDeps` when the extras aren't
installed — never a raw ``ImportError`` stack trace at scan time.

Testability
-----------
Real OTLP exporters need a listening collector. Tests inject an
in-memory ``SpanExporter`` via :meth:`OtelBeacon.for_testing` and read
the finished spans directly — no network, no daemon threads leaking
across tests.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..state import STATE_DIR_NAME
from .base import BeaconRecord

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter, SpanProcessor
    from opentelemetry.trace import Tracer


DEAD_LETTER_FILE = "otel.dead"
DEFAULT_SERVICE_NAME = "canary-cage"
INSTRUMENTATION_NAME = "canary_cage.beacons.otel"

# Attribute names — deliberately dotted + stable so downstream queries
# ("canary.type = manifest") don't churn every release.
ATTR_ID = "canary.id"
ATTR_TYPE = "canary.type"
ATTR_SOURCE = "canary.source"
ATTR_DETAIL = "canary.detail"
ATTR_PATH = "canary.path"
ATTR_DETECTED_AT = "canary.detected_at"
ATTR_REPO = "canary.repo"
ATTR_COMMIT = "canary.commit"

EVENT_NAME = "canary.fire"


class OtelBeaconMissingDeps(RuntimeError):
    """Raised when the ``opentelemetry`` SDK isn't installed.

    The message points to the ``canary-cage[otel]`` extra so operators
    get a one-line fix instead of a raw import stack trace.
    """


def _install_hint() -> str:
    return (
        "OpenTelemetry SDK not installed. Run "
        "`pip install 'canary-cage[otel]'` (or "
        "`uv pip install 'canary-cage[otel]'`) to enable the otel beacon."
    )


def dead_letter_path(root: Path) -> Path:
    return root / STATE_DIR_NAME / DEAD_LETTER_FILE


def _import_sdk() -> Any:
    """Import ``opentelemetry`` bits or raise :class:`OtelBeaconMissingDeps`.

    Kept as a single function so tests can monkeypatch it and the CLI
    can defer the import cost until the beacon is actually used.
    """
    try:
        from opentelemetry import trace as _trace  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise OtelBeaconMissingDeps(_install_hint()) from exc

    return {
        "trace": _trace,
        "Resource": Resource,
        "TracerProvider": TracerProvider,
        "BatchSpanProcessor": BatchSpanProcessor,
        "SimpleSpanProcessor": SimpleSpanProcessor,
    }


def _default_otlp_exporter() -> Any:
    """Build the default OTLP HTTP exporter honouring env vars.

    We pick the HTTP/protobuf exporter as the default because it works
    against every OTLP endpoint (Honeycomb, Grafana Cloud, self-hosted
    collectors) without extra ``grpcio`` deps. Callers who want gRPC can
    inject their own exporter via :meth:`OtelBeacon.for_testing` or by
    setting ``OTEL_EXPORTER_OTLP_PROTOCOL=grpc`` (which the exporter
    honours automatically).
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
    except ImportError as exc:
        raise OtelBeaconMissingDeps(_install_hint()) from exc
    return OTLPSpanExporter()


def _record_attributes(record: BeaconRecord, extra: dict[str, str]) -> dict[str, Any]:
    """Return the flat attribute dict we hang on the span + event."""
    attrs: dict[str, Any] = {
        ATTR_ID: record.canary_id,
        ATTR_TYPE: record.canary_type,
        ATTR_SOURCE: record.source,
        ATTR_DETAIL: record.detail,
        ATTR_DETECTED_AT: record.detected_at.isoformat(),
    }
    if record.path:
        attrs[ATTR_PATH] = record.path
    for key, value in extra.items():
        if value:
            attrs[key] = value
    return attrs


def _detected_at_ns(record: BeaconRecord) -> int:
    """Convert the record timestamp to the ns the OTel API expects."""
    return int(record.detected_at.timestamp() * 1_000_000_000)


@dataclass
class OtelBeacon:
    """Emit a span + ``canary.fire`` event per detected canary fire.

    The beacon lazily wires up a private :class:`TracerProvider` on
    first use so importing :mod:`canary_cage.beacons` is cheap even when
    the OTel extras aren't installed. Any exception during export is
    swallowed and appended to a JSON dead-letter — beacons must never
    raise into the scanner.
    """

    enabled: bool = True
    service_name: str = DEFAULT_SERVICE_NAME
    resource_attributes: dict[str, str] = field(default_factory=dict)
    name: str = "otel"

    # Populated lazily by :meth:`_ensure_tracer` or eagerly by
    # :meth:`for_testing`. Kept as ``Any`` so importing this module
    # doesn't touch the OTel SDK.
    _tracer: Any = field(default=None, repr=False, compare=False)
    _provider: Any = field(default=None, repr=False, compare=False)
    _processor: Any = field(default=None, repr=False, compare=False)
    _import_error: Exception | None = field(default=None, repr=False, compare=False)
    _shutdown: bool = field(default=False, repr=False, compare=False)

    # -----------------------------------------------------------------
    # Constructors
    # -----------------------------------------------------------------
    @classmethod
    def for_testing(
        cls,
        exporter: SpanExporter,
        *,
        service_name: str = DEFAULT_SERVICE_NAME,
        resource_attributes: dict[str, str] | None = None,
    ) -> OtelBeacon:
        """Build a beacon wired to ``exporter`` via a SimpleSpanProcessor.

        Using ``SimpleSpanProcessor`` means every span is flushed
        synchronously, so tests can assert on ``exporter.get_finished_spans()``
        immediately after ``beacon.fire(...)``. Callers own the exporter
        and are responsible for shutting it down.
        """
        sdk = _import_sdk()
        provider = sdk["TracerProvider"](
            resource=sdk["Resource"].create(
                {
                    "service.name": service_name,
                    **(resource_attributes or {}),
                }
            )
        )
        processor = sdk["SimpleSpanProcessor"](exporter)
        provider.add_span_processor(processor)
        beacon = cls(
            enabled=True,
            service_name=service_name,
            resource_attributes=dict(resource_attributes or {}),
        )
        beacon._provider = provider
        beacon._processor = processor
        beacon._tracer = provider.get_tracer(INSTRUMENTATION_NAME)
        return beacon

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def _ensure_tracer(self) -> Tracer | None:
        """Lazily wire up the tracer. Returns ``None`` on hard failure."""
        if self._shutdown:
            return None
        if self._tracer is not None:
            return self._tracer
        if self._import_error is not None:
            return None
        try:
            sdk = _import_sdk()
        except OtelBeaconMissingDeps as exc:
            self._import_error = exc
            return None

        provider = sdk["TracerProvider"](
            resource=sdk["Resource"].create(
                {
                    "service.name": self.service_name,
                    **self.resource_attributes,
                }
            )
        )
        try:
            exporter = _default_otlp_exporter()
        except OtelBeaconMissingDeps as exc:
            self._import_error = exc
            return None
        processor = sdk["BatchSpanProcessor"](exporter)
        provider.add_span_processor(processor)
        self._provider = provider
        self._processor = processor
        self._tracer = provider.get_tracer(INSTRUMENTATION_NAME)
        return self._tracer

    def shutdown(self) -> None:
        """Flush + release the underlying provider (best-effort).

        After shutdown the beacon is inert — subsequent :meth:`fire`
        calls silently no-op. This matches how OTel providers behave
        upstream and prevents surprise re-arming during tests.
        """
        self._shutdown = True
        provider = self._provider
        if provider is None:
            self._tracer = None
            self._processor = None
            return
        try:
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        self._provider = None
        self._processor = None
        self._tracer = None

    # -----------------------------------------------------------------
    # Beacon protocol
    # -----------------------------------------------------------------
    def fire(self, root: Path, record: BeaconRecord) -> None:
        if not self.enabled:
            return

        tracer = self._ensure_tracer()
        if tracer is None:
            error = self._import_error or RuntimeError(_install_hint())
            self._write_dead_letter(root, record, str(error))
            return

        extra: dict[str, str] = {}
        repo = _git_repo_name(root)
        if repo:
            extra[ATTR_REPO] = repo
        commit = _git_head_sha(root)
        if commit:
            extra[ATTR_COMMIT] = commit

        attrs = _record_attributes(record, extra)
        start_ns = _detected_at_ns(record)

        try:
            span_ctx = tracer.start_as_current_span(
                EVENT_NAME,
                start_time=start_ns,
                attributes=attrs,
            )
            with span_ctx as span:
                span.add_event(EVENT_NAME, attributes=attrs, timestamp=start_ns)
                span.set_attribute("canary.fired", True)
        except Exception as exc:  # noqa: BLE001 - beacons must not raise
            self._write_dead_letter(root, record, f"{type(exc).__name__}: {exc}")

    # -----------------------------------------------------------------
    # Dead-letter (mirrors webhook/chat beacons)
    # -----------------------------------------------------------------
    def _write_dead_letter(self, root: Path, record: BeaconRecord, error: str) -> None:
        path = dead_letter_path(root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "service_name": self.service_name,
                    "error": error,
                    "record": record.model_dump(mode="json"),
                },
                sort_keys=True,
            )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            # If we can't even write the dead-letter, swallow — the
            # scanner should never crash because a beacon failed.
            return


# ---------------------------------------------------------------------------
# Small git helpers — best-effort, silent on failure.
# ---------------------------------------------------------------------------


def _git_repo_name(root: Path) -> str:
    """Infer a repo name for the ``canary.repo`` attribute.

    Falls back to the root's directory name if there's no git remote —
    single, boring string that lets you group fires per project without
    forcing OTLP resource-detector setup.
    """
    if not (root / ".git").exists():
        return root.name
    try:
        import subprocess  # noqa: PLC0415

        proc = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return root.name
    url = (proc.stdout or "").strip()
    if not url:
        return root.name
    # Normalise "git@github.com:owner/repo.git" → "owner/repo"
    stripped = url.split("://", 1)[-1]
    if ":" in stripped and "@" in stripped:
        stripped = stripped.split(":", 1)[-1]
    if stripped.endswith(".git"):
        stripped = stripped[: -len(".git")]
    return stripped or root.name


def _git_head_sha(root: Path) -> str:
    """Return the current HEAD sha (short), or ``""`` when unavailable."""
    if not (root / ".git").exists():
        return ""
    # Allow tests / CI to short-circuit repo lookups.
    override = os.environ.get("CANARY_CAGE_TEST_HEAD_SHA")
    if override is not None:
        return override
    try:
        import subprocess  # noqa: PLC0415

        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return ""
    return (proc.stdout or "").strip()


__all__ = [
    "ATTR_COMMIT",
    "ATTR_DETAIL",
    "ATTR_DETECTED_AT",
    "ATTR_ID",
    "ATTR_PATH",
    "ATTR_REPO",
    "ATTR_SOURCE",
    "ATTR_TYPE",
    "DEFAULT_SERVICE_NAME",
    "EVENT_NAME",
    "INSTRUMENTATION_NAME",
    "OtelBeacon",
    "OtelBeaconMissingDeps",
    "dead_letter_path",
]


# Types used only for hints — keep the module importable without OTel.
if TYPE_CHECKING:  # pragma: no cover
    SpanExporter = "SpanExporter"  # type: ignore[assignment]
    SpanProcessor = "SpanProcessor"  # type: ignore[assignment]
    Tracer = "Tracer"  # type: ignore[assignment]
    TracerProvider = "TracerProvider"  # type: ignore[assignment]
