"""FastAPI dependency tests for require_jwt_scope (#116, RFC 0002)."""

from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from tests.auth.conftest import build_auth_app as _build_app


@pytest.fixture
def signing_keys() -> Any:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public.public_bytes(
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


def test_returns_anonymous_when_auth_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    app = _build_app()
    client = TestClient(app)
    r = client.post("/execute")
    assert r.status_code == 200
    assert r.json() == {"tenant": "anonymous"}


def test_rejects_when_required_and_no_token(
    monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> None:
    _, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    app = _build_app()
    r = TestClient(app).post("/execute")
    assert r.status_code == 401


def test_accepts_valid_token_with_required_scope(
    monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> None:
    private_pem, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    token = _issue(private_pem, scopes=["exec:fn"])
    r = TestClient(_build_app()).post("/execute", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"tenant": "tenant-acme"}


def test_rejects_token_missing_required_scope(
    monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> None:
    private_pem, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    # Token has exec:fn but the /admin handler wants admin:credits
    token = _issue(private_pem, scopes=["exec:fn"])
    r = TestClient(_build_app()).get("/admin", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_rejects_token_for_wrong_worker(monkeypatch: pytest.MonkeyPatch, signing_keys: Any) -> None:
    private_pem, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-2")
    token = _issue(private_pem, scopes=["exec:fn"], worker_id="worker-1")
    r = TestClient(_build_app()).post("/execute", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_rejects_malformed_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", "-----BEGIN PUBLIC KEY-----")
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    r = TestClient(_build_app()).post("/execute", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401


def test_auth_required_delegates_to_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    from zakuro.auth import middleware
    from zakuro.worker import posture

    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    assert middleware._auth_required() is posture.is_auth_required() is True
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "0")
    assert middleware._auth_required() is posture.is_auth_required() is False
