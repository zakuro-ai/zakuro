"""Unit tests for the OpenTelemetry tracing surface (#123, RFC 0003 Track C)."""

from __future__ import annotations

from typing import Any

import pytest

from zakuro.observability import tracing as obs_tracing

pytest.importorskip("opentelemetry.sdk", reason="needs [observability] extra")


@pytest.fixture(autouse=True)
def _reset_tracing(monkeypatch: pytest.MonkeyPatch) -> Any:
    obs_tracing._reset_for_tests()
    # Avoid leaking real env into tests
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER_ARG", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    yield
    # Drop the global provider so the background BatchSpanProcessor stops
    # retrying export against the fake endpoint and polluting the test log.
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = _trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
        _trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    obs_tracing._reset_for_tests()


def test_is_enabled_reports_sdk_presence() -> None:
    assert obs_tracing.is_enabled() is True


def test_init_tracing_noop_when_endpoint_unset() -> None:
    assert obs_tracing.init_tracing("worker") is False


def test_init_tracing_wires_provider_when_endpoint_set() -> None:
    assert obs_tracing.init_tracing("worker", endpoint="http://127.0.0.1:1") is True


def test_init_tracing_is_idempotent() -> None:
    obs_tracing.init_tracing("worker", endpoint="http://127.0.0.1:1")
    # second call must not raise; returns the enabled bool
    assert obs_tracing.init_tracing("worker", endpoint="http://127.0.0.1:1") is True


def test_get_tracer_returns_a_usable_object() -> None:
    obs_tracing.init_tracing("worker", endpoint="http://127.0.0.1:1")
    tracer = obs_tracing.get_tracer("zakuro.test")
    with tracer.start_as_current_span("zakuro.worker.execute") as span:
        span.set_attribute("tenant_id", "tenant-acme")


def test_get_tracer_returns_noop_when_not_initialised() -> None:
    """If init_tracing wasn't called, get_tracer still returns something usable.

    The fallback path is exercised by passing through the noop tracer in the
    no-endpoint case so caller code doesn't have to special-case enabled vs not.
    """
    tracer = obs_tracing.get_tracer("zakuro.test")
    # Real tracer from a still-initialised global provider is OK too — both
    # must be safe to use. We just check the context manager works.
    with tracer.start_as_current_span("zakuro.worker.execute") as span:
        span.set_attribute("k", "v")


def test_parse_resource_attributes() -> None:
    assert obs_tracing._parse_resource_attributes("a=1,b=2,empty") == {"a": "1", "b": "2"}
    assert obs_tracing._parse_resource_attributes("") == {}
    assert obs_tracing._parse_resource_attributes(" key = value ") == {"key": "value"}


def test_sample_rate_overrides() -> None:
    obs_tracing._reset_for_tests()
    assert obs_tracing.init_tracing("worker", endpoint="http://c", sample_rate=1.0) is True
