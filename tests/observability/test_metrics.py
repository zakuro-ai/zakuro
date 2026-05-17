"""Unit tests for the Prometheus metrics surface (#124, RFC 0003 Track B)."""

from __future__ import annotations

from typing import Any

import pytest

from zakuro.observability import metrics as obs_metrics

prom = pytest.importorskip("prometheus_client", reason="needs [observability] extra")


@pytest.fixture(autouse=True)
def _reset_metrics() -> Any:
    obs_metrics._reset_for_tests()
    yield
    obs_metrics._reset_for_tests()


def test_init_metrics_is_idempotent_and_signals_enabled() -> None:
    assert obs_metrics.init_metrics() is True
    assert obs_metrics.init_metrics() is True
    assert obs_metrics.is_enabled() is True


def test_record_http_request_increments_counter() -> None:
    obs_metrics.init_metrics()
    obs_metrics.record_http_request("POST", "/execute", 200, "tenant-acme")
    obs_metrics.record_http_request("POST", "/execute", 200, "tenant-acme")
    obs_metrics.record_http_request("POST", "/execute", 500, "tenant-acme")

    body = prom.generate_latest(prom.REGISTRY).decode()
    assert (
        'zakuro_http_requests_total{method="POST",path="/execute",status="200",tenant_id="tenant-acme"} 2.0'
        in body
    )
    assert (
        'zakuro_http_requests_total{method="POST",path="/execute",status="500",tenant_id="tenant-acme"} 1.0'
        in body
    )


def test_observe_dispatch_latency_records_histogram_sample() -> None:
    obs_metrics.init_metrics()
    stop = obs_metrics.observe_dispatch_latency("worker-1", "tenant-acme")
    stop()

    body = prom.generate_latest(prom.REGISTRY).decode()
    assert "zakuro_dispatch_latency_seconds_count" in body
    # one sample observed → count for the labelset is 1
    assert (
        'zakuro_dispatch_latency_seconds_count{tenant_id="tenant-acme",worker_id="worker-1"} 1.0'
        in body
    )


def test_set_worker_queue_depth_updates_gauge() -> None:
    obs_metrics.init_metrics()
    obs_metrics.set_worker_queue_depth("worker-1", 7)
    body = prom.generate_latest(prom.REGISTRY).decode()
    assert 'zakuro_worker_queue_depth{worker_id="worker-1"} 7.0' in body
    obs_metrics.set_worker_queue_depth("worker-1", 0)
    body = prom.generate_latest(prom.REGISTRY).decode()
    assert 'zakuro_worker_queue_depth{worker_id="worker-1"} 0.0' in body


def test_record_auth_failure_increments_counter() -> None:
    obs_metrics.init_metrics()
    obs_metrics.record_auth_failure("missing_jwt", "tenant-acme")
    obs_metrics.record_auth_failure("missing_jwt", "tenant-acme")
    obs_metrics.record_auth_failure("bad_scope", "tenant-acme")

    body = prom.generate_latest(prom.REGISTRY).decode()
    assert 'zakuro_auth_failure_total{reason="missing_jwt",tenant_id="tenant-acme"} 2.0' in body
    assert 'zakuro_auth_failure_total{reason="bad_scope",tenant_id="tenant-acme"} 1.0' in body


def test_mount_metrics_endpoint_serves_exposition() -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    obs_metrics.init_metrics()
    obs_metrics.mount_metrics_endpoint(app)
    obs_metrics.set_worker_queue_depth("worker-1", 3)

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "zakuro_worker_queue_depth" in resp.text


def test_metric_names_follow_prometheus_conventions() -> None:
    """All declared metric names use <namespace>_<subsystem>_<name>_<unit>."""
    for metric in (
        obs_metrics.HTTP_REQUESTS_TOTAL,
        obs_metrics.DISPATCH_LATENCY_SECONDS,
        obs_metrics.WORKER_QUEUE_DEPTH,
        obs_metrics.AUTH_FAILURE_TOTAL,
    ):
        name = metric._name  # noqa: SLF001 — prometheus_client exposes _name
        assert name.startswith("zakuro_"), f"metric {name} missing zakuro_ namespace"
