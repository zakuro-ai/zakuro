"""Public 0.2.24 auth names kept for compatibility (ported from zakuro-ai/zakuro tests/auth)."""

from __future__ import annotations

import pytest

from zakuro.auth import derive_payload_hmac_key as exported
from zakuro.auth.jwt import JwtError, derive_payload_hmac_key
from zakuro.wire.keys import derive_payload_hmac_key as wire_derive


def test_derive_payload_hmac_key_is_deterministic() -> None:
    key = b"x" * 32
    a = derive_payload_hmac_key(jwt_signing_key=key, tenant_id="tenant-acme")
    b = derive_payload_hmac_key(jwt_signing_key=key, tenant_id="tenant-acme")
    assert a == b
    assert len(a) == 32
    assert exported is derive_payload_hmac_key


def test_derive_payload_hmac_key_diverges_by_tenant_and_key() -> None:
    key = b"x" * 32
    assert derive_payload_hmac_key(
        jwt_signing_key=key, tenant_id="tenant-a"
    ) != derive_payload_hmac_key(jwt_signing_key=key, tenant_id="tenant-b")
    assert derive_payload_hmac_key(
        jwt_signing_key=b"a" * 32, tenant_id="t"
    ) != derive_payload_hmac_key(jwt_signing_key=b"b" * 32, tenant_id="t")


def test_derive_payload_hmac_key_matches_wire_keys_derivation() -> None:
    """Same HKDF as zakuro.wire.keys, so the worker and broker agree on the tenant key."""
    key = bytes(range(32))
    assert derive_payload_hmac_key(jwt_signing_key=key, tenant_id="t") == wire_derive(
        master_key=key, tenant_id="t"
    )


def test_derive_payload_hmac_key_rejects_empty_key() -> None:
    with pytest.raises(JwtError):
        derive_payload_hmac_key(jwt_signing_key=b"", tenant_id="t")


def test_auth_required_delegates_to_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    from zakuro.auth import middleware
    from zakuro.worker import posture

    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    assert middleware._auth_required() is posture.is_auth_required() is True
    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED")
    assert middleware._auth_required() is posture.is_auth_required() is False
    assert middleware._ANONYMOUS_CLAIMS is middleware.ANONYMOUS_CLAIMS


def test_execute_chunk_opcode_requires_exec_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """OP_EXECUTE_CHUNK (4) runs code, so it needs a token exactly like OP_EXECUTE."""
    from zakuro.auth.frame import FrameAuthError, authorize_frame

    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    with pytest.raises(FrameAuthError):
        authorize_frame(4, b"chunk")
