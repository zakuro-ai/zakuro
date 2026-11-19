"""Tests for the /live, /ready, /health probes (closes #126)."""

from __future__ import annotations

import pytest

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    pytest.skip("FastAPI testclient not available", allow_module_level=True)

from zakuro.worker import server as worker_server


@pytest.fixture
def client():
    """Boot the FastAPI app once per test with a clean executor state."""
    with TestClient(worker_server.app) as c:
        yield c


# ---------------------------------------------------------------------------
# /live
# ---------------------------------------------------------------------------


def test_live_returns_200_when_loop_is_responsive(client):
    r = client.get("/live")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_live_returns_503_when_loop_check_fails(client, monkeypatch):
    async def _hung():
        return False

    monkeypatch.setattr(worker_server, "_check_event_loop_responsive", _hung)
    r = client.get("/live")
    assert r.status_code == 503
    assert "unresponsive" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /ready — happy path
# ---------------------------------------------------------------------------


def test_ready_returns_200_when_no_deps_configured(client, monkeypatch):
    """No broker / no storage env vars → ready cares only about executor."""
    for var in (
        "ZAKURO_BROKER_URL",
        "ZAKURO_S3_ENDPOINT",
        "ZAKURO_S3_ACCESS_KEY",
        "ZAKURO_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["executor"] == "ready"
    assert body["checks"]["broker"] == "ready"
    assert body["checks"]["storage"] == "ready"
    assert body["failures"] == []


# ---------------------------------------------------------------------------
# /ready — deep-probe failure modes
# ---------------------------------------------------------------------------


def test_ready_returns_503_when_executor_not_initialised(client, monkeypatch):
    """If the FastAPI startup event hasn't wired the executor, we are not ready."""
    monkeypatch.setattr(worker_server, "executor", None)

    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not-ready"
    assert body["checks"]["executor"] == "not-ready"
    assert any("executor" in f for f in body["failures"])


def test_ready_returns_503_when_broker_unreachable(client, monkeypatch):
    """Broker configured but failing → /ready 503."""

    async def _broker_fail():
        return False, "broker http://broker:9000 unreachable: ConnectionRefusedError"

    monkeypatch.setattr(worker_server, "_check_broker_reachable", _broker_fail)
    monkeypatch.setenv("ZAKURO_BROKER_URL", "http://broker:9000")

    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert "unreachable" in body["checks"]["broker"]
    assert any("unreachable" in f for f in body["failures"])


def test_ready_returns_503_when_storage_unreachable(client, monkeypatch):
    """MinIO chaos test: storage down → /ready 503, but /live still 200."""

    async def _storage_fail():
        return False, "storage minio:9000 timed out (2s)"

    monkeypatch.setattr(worker_server, "_check_storage_reachable", _storage_fail)
    monkeypatch.setenv("ZAKURO_S3_ENDPOINT", "minio:9000")
    monkeypatch.setenv("ZAKURO_S3_ACCESS_KEY", "x")
    monkeypatch.setenv("ZAKURO_S3_SECRET_KEY", "y")

    # /ready fails
    r_ready = client.get("/ready")
    assert r_ready.status_code == 503
    assert "timed out" in r_ready.json()["checks"]["storage"]

    # /live still passes — this is the key invariant of #126
    r_live = client.get("/live")
    assert r_live.status_code == 200


def test_ready_aggregates_multiple_failures(client, monkeypatch):
    """Both broker and storage down → both failures listed."""

    async def _broker_fail():
        return False, "broker unreachable"

    async def _storage_fail():
        return False, "storage unreachable"

    monkeypatch.setattr(worker_server, "_check_broker_reachable", _broker_fail)
    monkeypatch.setattr(worker_server, "_check_storage_reachable", _storage_fail)

    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert len(body["failures"]) == 2


# ---------------------------------------------------------------------------
# /health — backwards compatibility
# ---------------------------------------------------------------------------


def test_health_aliases_ready(client, monkeypatch):
    """Existing Docker HEALTHCHECK consumers must keep working."""
    r_health = client.get("/health")
    r_ready = client.get("/ready")
    assert r_health.status_code == r_ready.status_code
    assert r_health.json()["status"] == r_ready.json()["status"]
