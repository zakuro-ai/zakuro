"""verify_jwt / authorize_token unit tests (ADR 0013, #84)."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from tests.auth.conftest import issue_token
from zakuro.auth import (
    JwtError,
    JwtExpiredError,
    JwtScopeError,
    JwtSignatureError,
    authorize_token,
    verify_jwt,
)


def test_happy_path(signing_keys: tuple[bytes, bytes]) -> None:
    priv, pub = signing_keys
    claims = verify_jwt(
        issue_token(priv, scopes=["exec:fn"]), expected_audience="zakuro-worker", public_key_pem=pub
    )
    assert claims.tenant_id == "tenant-acme"
    assert claims.has_scope("exec:fn")
    assert not claims.has_scope("admin")


def test_expired(signing_keys: tuple[bytes, bytes]) -> None:
    priv, pub = signing_keys
    with pytest.raises(JwtExpiredError):
        verify_jwt(
            issue_token(priv, scopes=[], exp_delta=-10),
            expected_audience="zakuro-worker",
            public_key_pem=pub,
        )


def test_wrong_audience(signing_keys: tuple[bytes, bytes]) -> None:
    priv, pub = signing_keys
    with pytest.raises(JwtSignatureError):
        verify_jwt(
            issue_token(priv, scopes=[], aud="dashboard"),
            expected_audience="zakuro-worker",
            public_key_pem=pub,
        )


def test_alg_confusion_rejected(signing_keys: tuple[bytes, bytes]) -> None:
    _, pub = signing_keys
    now = int(time.time())
    forged = pyjwt.encode(
        {"aud": "zakuro-worker", "exp": now + 60, "nbf": now, "jti": "x",
         "tenant_id": "t", "worker_id": "w", "scopes": ["exec:fn"]},
        b"any-secret-that-is-long-enough-for-hs256",
        algorithm="HS256",
    )  # fmt: skip
    with pytest.raises(JwtSignatureError):
        verify_jwt(forged, expected_audience="zakuro-worker", public_key_pem=pub)


def test_missing_key_refuses_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_JWT_PUBLIC_KEY_PEM", raising=False)
    with pytest.raises(JwtSignatureError):
        verify_jwt("x.y.z", expected_audience="zakuro-worker")


def test_missing_claims(signing_keys: tuple[bytes, bytes]) -> None:
    priv, pub = signing_keys
    now = int(time.time())
    token = pyjwt.encode(
        {"aud": "zakuro-worker", "exp": now + 60, "jti": "x"}, priv, algorithm="EdDSA"
    )
    with pytest.raises(JwtError):
        verify_jwt(token, expected_audience="zakuro-worker", public_key_pem=pub)


def test_authorize_token_scope_and_worker(auth_env: bytes) -> None:
    assert authorize_token(issue_token(auth_env, scopes=["exec:fn"]), "exec:fn").jti == "j-1"
    with pytest.raises(JwtScopeError):
        authorize_token(issue_token(auth_env, scopes=["exec:fn"]), "worker:info")
    with pytest.raises(JwtSignatureError):
        authorize_token(issue_token(auth_env, scopes=["exec:fn"], worker_id="other"), "exec:fn")
