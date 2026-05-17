"""Phase-2 wiring tests: /execute + /info require JWT scopes (#116)."""

from __future__ import annotations

import time
from typing import Any

import cloudpickle
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient


@pytest.fixture
def signing_keys() -> Any:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _issue(private_pem: bytes, *, scopes: list[str], worker_id: str = "worker-1") -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "aud": "zakuro-worker",
            "exp": now + 900,
            "nbf": now,
            "jti": "j-1",
            "tenant_id": "tenant-acme",
            "worker_id": worker_id,
            "scopes": scopes,
        },
        private_pem,
        algorithm="EdDSA",
    )


def _client() -> TestClient:
    from zakuro.worker.server import app

    return TestClient(app)


def _square_body(value: int) -> bytes:
    def f(x: int) -> int:
        return x * x

    return cloudpickle.dumps({"action": "execute", "func": f, "args": (value,)})


# ---- legacy / anonymous (ZAKURO_AUTH_REQUIRED unset) -------------------------


def test_execute_anonymous_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth not required → no token → /execute still runs."""
    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    with _client() as c:
        r = c.post("/execute", content=_square_body(9))
    assert r.status_code == 200
    assert cloudpickle.loads(r.content) == 81


def test_info_anonymous_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    with _client() as c:
        r = c.get("/info")
    assert r.status_code == 200
    body = r.json()
    # Response shape is owned by the existing /info handler; we just
    # need to confirm the auth gate didn't 401 us.
    assert isinstance(body, dict)
    assert "cpus_total" in body or "resources" in body or "name" in body


# ---- strict mode (ZAKURO_AUTH_REQUIRED=1) -----------------------------------


def test_execute_strict_no_token_returns_401(
    monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> None:
    _, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    with _client() as c:
        r = c.post("/execute", content=_square_body(9))
    assert r.status_code == 401


def test_execute_strict_valid_token_passes(
    monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> None:
    private_pem, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    token = _issue(private_pem, scopes=["exec:fn"])
    with _client() as c:
        r = c.post(
            "/execute",
            content=_square_body(9),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    assert cloudpickle.loads(r.content) == 81


def test_execute_strict_wrong_scope_returns_401(
    monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> None:
    private_pem, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    # Token has admin:credits but /execute wants exec:fn
    token = _issue(private_pem, scopes=["admin:credits"])
    with _client() as c:
        r = c.post(
            "/execute",
            content=_square_body(9),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 401


def test_info_strict_requires_info_scope(
    monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> None:
    private_pem, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    # Token has exec:fn (good for /execute) but /info wants worker:info
    exec_token = _issue(private_pem, scopes=["exec:fn"])
    info_token = _issue(private_pem, scopes=["worker:info"])
    with _client() as c:
        r_no_scope = c.get("/info", headers={"Authorization": f"Bearer {exec_token}"})
        r_with_scope = c.get("/info", headers={"Authorization": f"Bearer {info_token}"})
    assert r_no_scope.status_code == 401
    assert r_with_scope.status_code == 200


def test_live_ready_remain_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    """/live and /ready intentionally have no auth; kubelet probes hit them."""
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", "-----BEGIN PUBLIC KEY-----")
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    with _client() as c:
        r_live = c.get("/live")
        r_ready = c.get("/ready")
    # /live always returns 200 unless event loop is dead
    assert r_live.status_code == 200
    # /ready may 503 in this minimal context (deps not configured) but
    # crucially must NOT return 401 — it's pre-auth.
    assert r_ready.status_code != 401
