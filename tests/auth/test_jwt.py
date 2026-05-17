"""Unit tests for JWT verification + HKDF derivation (#116, RFC 0002)."""

from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zakuro.auth.jwt import (
    Claims,
    JwtError,
    JwtExpiredError,
    JwtSignatureError,
    derive_payload_hmac_key,
    verify_jwt,
)


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


def _issue(
    private_pem: bytes,
    *,
    tenant_id: str = "tenant-acme",
    worker_id: str = "worker-1",
    scopes: list[str] | None = None,
    aud: str = "zakuro-worker",
    exp_delta: int = 900,
    nbf_delta: int = 0,
    jti: str = "j-1",
) -> str:
    now = int(time.time())
    payload = {
        "iss": "broker",
        "aud": aud,
        "iat": now,
        "nbf": now + nbf_delta,
        "exp": now + exp_delta,
        "jti": jti,
        "tenant_id": tenant_id,
        "worker_id": worker_id,
        "scopes": scopes if scopes is not None else ["exec:fn"],
    }
    return pyjwt.encode(payload, private_pem, algorithm="EdDSA")


def test_verify_jwt_happy_path(signing_keys: Any) -> None:
    private_pem, public_pem = signing_keys
    token = _issue(private_pem)
    claims = verify_jwt(token, expected_audience="zakuro-worker", public_key_pem=public_pem)
    assert isinstance(claims, Claims)
    assert claims.tenant_id == "tenant-acme"
    assert claims.has_scope("exec:fn")
    assert not claims.has_scope("admin:credits")


def test_verify_jwt_rejects_expired_token(signing_keys: Any) -> None:
    private_pem, public_pem = signing_keys
    token = _issue(private_pem, exp_delta=-10)
    with pytest.raises(JwtExpiredError):
        verify_jwt(token, expected_audience="zakuro-worker", public_key_pem=public_pem)


def test_verify_jwt_rejects_wrong_audience(signing_keys: Any) -> None:
    private_pem, public_pem = signing_keys
    token = _issue(private_pem, aud="zakuro-dashboard")
    with pytest.raises(JwtSignatureError):
        verify_jwt(token, expected_audience="zakuro-worker", public_key_pem=public_pem)


def test_verify_jwt_rejects_wrong_signing_key(signing_keys: Any) -> None:
    private_pem, _ = signing_keys
    # Generate a second, unrelated public key
    other_public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    token = _issue(private_pem)
    with pytest.raises(JwtSignatureError):
        verify_jwt(token, expected_audience="zakuro-worker", public_key_pem=other_public)


def test_verify_jwt_rejects_alg_confusion(signing_keys: Any) -> None:
    _, public_pem = signing_keys
    # Forge an HS256 token using *any* secret. The classic alg-confusion
    # attack relies on the verifier accepting HS256, regardless of which
    # secret was used. `algorithms=["EdDSA"]` blocks it at parse time.
    # (PyJWT itself refuses to use an asymmetric key as an HS256 secret
    # at encode time — so we encode with a plain bytes secret here.)
    now = int(time.time())
    forged = pyjwt.encode(
        {
            "aud": "zakuro-worker",
            "exp": now + 60,
            "nbf": now,
            "jti": "x",
            "tenant_id": "tenant",
            "worker_id": "worker",
            "scopes": ["exec:fn"],
        },
        b"any-secret",
        algorithm="HS256",
    )
    with pytest.raises(JwtSignatureError):
        verify_jwt(forged, expected_audience="zakuro-worker", public_key_pem=public_pem)


def test_verify_jwt_rejects_token_for_wrong_worker(signing_keys: Any) -> None:
    private_pem, public_pem = signing_keys
    token = _issue(private_pem, worker_id="worker-1")
    with pytest.raises(JwtSignatureError):
        verify_jwt(
            token,
            expected_audience="zakuro-worker",
            expected_worker_id="worker-2",
            public_key_pem=public_pem,
        )


def test_verify_jwt_rejects_missing_required_claims(signing_keys: Any) -> None:
    private_pem, public_pem = signing_keys
    now = int(time.time())
    # No `nbf` — verify_jwt requires it via options["require"]
    token = pyjwt.encode(
        {
            "aud": "zakuro-worker",
            "exp": now + 60,
            "jti": "x",
            "tenant_id": "tenant",
            "worker_id": "worker",
            "scopes": ["exec:fn"],
        },
        private_pem,
        algorithm="EdDSA",
    )
    with pytest.raises(JwtError):
        verify_jwt(token, expected_audience="zakuro-worker", public_key_pem=public_pem)


def test_verify_jwt_rejects_malformed_scopes(signing_keys: Any) -> None:
    private_pem, public_pem = signing_keys
    # scopes as a string rather than a list
    now = int(time.time())
    token = pyjwt.encode(
        {
            "aud": "zakuro-worker",
            "exp": now + 60,
            "nbf": now,
            "jti": "x",
            "tenant_id": "tenant",
            "worker_id": "worker",
            "scopes": "exec:fn",
        },
        private_pem,
        algorithm="EdDSA",
    )
    with pytest.raises(JwtError, match="scopes"):
        verify_jwt(token, expected_audience="zakuro-worker", public_key_pem=public_pem)


def test_derive_payload_hmac_key_is_deterministic() -> None:
    key = b"x" * 32
    a = derive_payload_hmac_key(jwt_signing_key=key, tenant_id="tenant-acme")
    b = derive_payload_hmac_key(jwt_signing_key=key, tenant_id="tenant-acme")
    assert a == b
    assert len(a) == 32


def test_derive_payload_hmac_key_diverges_by_tenant() -> None:
    key = b"x" * 32
    a = derive_payload_hmac_key(jwt_signing_key=key, tenant_id="tenant-a")
    b = derive_payload_hmac_key(jwt_signing_key=key, tenant_id="tenant-b")
    assert a != b


def test_derive_payload_hmac_key_diverges_by_signing_key() -> None:
    a = derive_payload_hmac_key(jwt_signing_key=b"a" * 32, tenant_id="t")
    b = derive_payload_hmac_key(jwt_signing_key=b"b" * 32, tenant_id="t")
    assert a != b
