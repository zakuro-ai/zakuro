"""OpenTelemetry tracing for Zakuro (closes #123 — RFC 0003 Track C).

Design notes:

* The single entry point is :func:`init_tracing`. It wires an OTLP/HTTP
  tracer provider against the standard ``OTEL_EXPORTER_OTLP_ENDPOINT``
  environment variable (so the same OTel-conformant env vars
  configure us as any other OTel SDK consumer).
* Sampling defaults to **10%** via :class:`TraceIdRatioBased`. Override
  via ``OTEL_TRACES_SAMPLER_ARG`` (e.g. ``0.5`` for 50%, ``1.0`` for
  always-on during incident investigation).
* When ``opentelemetry-sdk`` is not installed *or* the endpoint env var
  is unset, :func:`init_tracing` is a no-op that returns ``False``. The
  rest of the library still imports cleanly. This keeps the
  observability extra truly opt-in (matches :func:`init_sentry`,
  :func:`init_metrics`).
* :func:`instrument_fastapi_app` calls the FastAPI instrumentation if
  available so the worker's HTTP transport surface is covered without
  hand-rolled middleware.
* :func:`get_tracer` returns a tracer rooted at the ``zakuro.<component>``
  namespace. QUIC handlers and broker code start spans through it using
  the conventional names from the RFC:

      zakuro.client.execute
      zakuro.worker.execute
      zakuro.adaptive.pick_worker
      zakuro.wire.pack / unpack

  Trace-id is propagated via the W3C ``traceparent`` header on every
  outbound HTTP call once :func:`instrument_httpx` runs at startup —
  spans automatically stitch across broker -> worker hops.
"""

from __future__ import annotations

import os
from typing import Any

try:  # opentelemetry-sdk is an optional dependency
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    _ENABLED = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _ENABLED = False
    trace = None  # type: ignore[assignment]


_INITIALISED = False
_DEFAULT_SAMPLE_RATE = 0.1


def init_tracing(
    component: str,
    *,
    endpoint: str | None = None,
    sample_rate: float | None = None,
) -> bool:
    """Initialise OTLP/HTTP tracing for *component*.

    *component* names the service in the resource attributes
    (``service.name = "zakuro.<component>"``).

    *endpoint* overrides the resolved ``OTEL_EXPORTER_OTLP_ENDPOINT``.
    Pass an explicit value in tests to point at a local collector.

    *sample_rate* overrides ``OTEL_TRACES_SAMPLER_ARG`` (default 0.1).

    Returns ``True`` iff a tracer provider was actually wired in.
    Returns ``False`` when:

      - ``opentelemetry-sdk`` is not installed (extra missing), or
      - no endpoint is configured (worker is intentionally off-OTel).

    Safe to call multiple times — second call is a no-op.
    """
    global _INITIALISED
    if _INITIALISED:
        return _ENABLED
    if not _ENABLED:
        return False

    resolved_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not resolved_endpoint:
        # Caller explicitly didn't configure an endpoint — that's the
        # "off" signal. No tracer provider so spans become no-ops.
        return False

    if sample_rate is None:
        raw = os.environ.get("OTEL_TRACES_SAMPLER_ARG")
        sample_rate = _DEFAULT_SAMPLE_RATE if raw is None else _parse_float(raw)

    resource = Resource.create(
        {
            "service.name": f"zakuro.{component}",
            "service.version": _read_version(),
            **_parse_resource_attributes(os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")),
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=TraceIdRatioBased(max(0.0, min(1.0, sample_rate))),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=resolved_endpoint)))
    trace.set_tracer_provider(provider)
    _INITIALISED = True
    return True


def is_enabled() -> bool:
    """Return True iff opentelemetry-sdk is installed."""
    return _ENABLED


def get_tracer(name: str) -> Any:
    """Return an OTel tracer or a no-op stub when the SDK is missing."""
    if not _ENABLED:
        return _NoopTracer()
    return trace.get_tracer(name)


def instrument_fastapi_app(app: Any) -> bool:
    """Apply OTel auto-instrumentation to a FastAPI app, if the extra is installed.

    Returns ``True`` iff instrumentation was applied.
    """
    if not _ENABLED:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return False
    FastAPIInstrumentor.instrument_app(app)
    return True


def instrument_httpx() -> bool:
    """Apply OTel auto-instrumentation to httpx clients, if available.

    Auto-injects ``traceparent`` on every outbound HTTP call so spans
    stitch across the broker -> worker hop without code changes.
    """
    if not _ENABLED:
        return False
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        return False
    HTTPXClientInstrumentor().instrument()
    return True


def _parse_float(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_SAMPLE_RATE


def _parse_resource_attributes(raw: str) -> dict[str, str]:
    """Parse the W3C-style ``key1=value1,key2=value2`` env var."""
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _read_version() -> str:
    try:
        from zakuro import __version__
    except ImportError:
        return "0.0.0"
    return __version__


class _NoopTracer:
    """Minimal stand-in returned when the SDK is not installed."""

    def start_as_current_span(self, _name: str, **_kwargs: Any) -> _NoopSpanCtx:
        return _NoopSpanCtx()


class _NoopSpanCtx:
    def __enter__(self) -> _NoopSpanCtx:
        return self

    def __exit__(self, *_exc: Any) -> None: ...

    def set_attribute(self, _key: str, _value: Any) -> None: ...

    def record_exception(self, _exc: BaseException) -> None: ...


def _reset_for_tests() -> None:
    """Reset module init flag. Test-only — do not call in prod."""
    global _INITIALISED
    _INITIALISED = False
