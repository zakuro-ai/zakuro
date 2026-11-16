"""Shared fixtures for the JWT auth tests (ADR 0013, #84)."""

from __future__ import annotations

import time
from typing import Any

import pytest

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


@pytest.fixture
def signing_keys() -> tuple[bytes, bytes]:
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


def issue_token(
    private_pem: bytes,
    *,
    scopes: list[str],
    worker_id: str = "worker-1",
    exp_delta: int = 900,
    aud: str = "zakuro-worker",
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "aud": aud,
        "exp": now + exp_delta,
        "nbf": now,
        "jti": "j-1",
        "tenant_id": "tenant-acme",
        "worker_id": worker_id,
        "scopes": scopes,
    }
    return str(jwt.encode(claims, private_pem, algorithm="EdDSA"))


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch, signing_keys: tuple[bytes, bytes]) -> bytes:
    """Turn auth on with a known key; returns the private PEM for issuing tokens."""
    private_pem, public_pem = signing_keys
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", public_pem.decode())
    monkeypatch.setenv("ZAKURO_WORKER_ID", "worker-1")
    return private_pem
