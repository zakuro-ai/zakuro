"""HMAC key handling and client-side signing for the ``ZAKURO_WIRE=v1`` envelope.

The shared secret (the "master key") comes from ``ZAKURO_HMAC_KEY`` (hex) or
``ZAKURO_HMAC_KEY_FILE`` (raw bytes); setting both is an error. Each tenant
verifies against ``HKDF-SHA256(master, salt="zakuro-wire-v1",
info="payload:<tenant_id>")`` (RFC 5869, implemented with the stdlib so no new
dependency is needed). The key is never logged and never appears in error text.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from pathlib import Path

from zakuro.wire import (
    WIRE_VERSION_V1,
    Envelope,
    EnvelopeV2,
    ResourceLimits,
    WireError,
    compute_hmac,
    compute_hmac_v2,
    encode_envelope,
    encode_envelope_v2,
)

_HKDF_SALT = b"zakuro-wire-v1"
_HMAC_KEY_LENGTH = 32
DEFAULT_TENANT_ID = "default"


class WireKeyError(WireError):
    """The HMAC master key is missing or misconfigured."""


def wire_mode() -> str:
    """The configured ``ZAKURO_WIRE`` mode: ``"v1"``, ``"v2"``, or ``""`` (off/unknown)."""
    mode = os.environ.get("ZAKURO_WIRE", "").strip().lower()
    return mode if mode in ("v1", "v2") else ""


def is_wire_strict() -> bool:
    """True iff ``ZAKURO_WIRE`` is ``v1`` or ``v2`` -- the signed envelope is required."""
    return wire_mode() != ""


def read_master_key() -> bytes:
    """Read the per-mesh HMAC master key from the environment. Fails closed."""
    hex_key = os.environ.get("ZAKURO_HMAC_KEY", "").strip()
    key_file = os.environ.get("ZAKURO_HMAC_KEY_FILE", "").strip()
    if hex_key and key_file:
        raise WireKeyError("Both ZAKURO_HMAC_KEY and ZAKURO_HMAC_KEY_FILE are set; pick one.")
    key = b""
    if hex_key:
        try:
            key = bytes.fromhex(hex_key)
        except ValueError:
            raise WireKeyError("ZAKURO_HMAC_KEY is not valid hex") from None
    elif key_file:
        try:
            key = Path(key_file).expanduser().read_bytes().strip()
        except OSError as exc:
            raise WireKeyError(f"could not read ZAKURO_HMAC_KEY_FILE: {exc.strerror}") from None
    else:
        raise WireKeyError(
            "ZAKURO_WIRE is set but no HMAC master key configured; set ZAKURO_HMAC_KEY "
            "or ZAKURO_HMAC_KEY_FILE."
        )
    if not key:
        raise WireKeyError("HMAC master key is empty")
    return key


def derive_payload_hmac_key(*, master_key: bytes, tenant_id: str) -> bytes:
    """HKDF-SHA256 per-tenant key (RFC 5869, one 32-byte block)."""
    if not master_key:
        raise WireKeyError("master key must not be empty")
    prk = hmac.new(_HKDF_SALT, master_key, hashlib.sha256).digest()
    info = f"payload:{tenant_id}".encode()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:_HMAC_KEY_LENGTH]


def derive_payload_hmac_key_v2(*, master_key: bytes, tenant_id: str) -> bytes:
    """v2 per-tenant key: same HKDF, info ``payload-v2:<tenant>`` so v1/v2 MACs differ."""
    if not master_key:
        raise WireKeyError("master key must not be empty")
    prk = hmac.new(_HKDF_SALT, master_key, hashlib.sha256).digest()
    info = f"payload-v2:{tenant_id}".encode()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:_HMAC_KEY_LENGTH]


def sign_payload_v2(
    callable_bytes: bytes,
    *,
    master_key: bytes,
    args: bytes = b"",
    job_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    issued_at_ms: int | None = None,
) -> bytes:
    """Encode and sign *callable_bytes* as a v2 envelope (ZAK-0020)."""
    job_id = job_id or uuid.uuid4().hex
    if issued_at_ms is None:
        issued_at_ms = int(time.time() * 1000)
    key = derive_payload_hmac_key_v2(master_key=master_key, tenant_id=tenant_id)
    mac = compute_hmac_v2(
        hmac_key=key,
        tenant_id=tenant_id,
        job_id=job_id,
        issued_at_ms=issued_at_ms,
        callable_bytes=callable_bytes,
        args=args,
    )
    return encode_envelope_v2(
        EnvelopeV2(
            job_id=job_id,
            tenant_id=tenant_id,
            issued_at_ms=issued_at_ms,
            callable=callable_bytes,
            args=args,
            hmac=mac,
        )
    )


def sign_payload(
    callable_bytes: bytes,
    *,
    master_key: bytes,
    args: bytes = b"",
    job_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bytes:
    """Encode and sign *callable_bytes* as a v1 envelope ready to POST."""
    job_id = job_id or uuid.uuid4().hex
    tenant_key = derive_payload_hmac_key(master_key=master_key, tenant_id=tenant_id)
    mac = compute_hmac(hmac_key=tenant_key, callable_bytes=callable_bytes, args=args, job_id=job_id)
    return encode_envelope(
        Envelope(
            version=WIRE_VERSION_V1,
            job_id=job_id,
            tenant_id=tenant_id,
            callable=callable_bytes,
            args=args,
            hmac=mac,
            resource_limits=ResourceLimits(cpus=1.0, memory_mb=1024, gpus=0, timeout_seconds=60),
        )
    )


def wrap_payload(payload: bytes) -> bytes:
    """Client hook: sign *payload* when ``ZAKURO_WIRE`` is ``v1``/``v2``, else return it unchanged."""
    mode = wire_mode()
    if not mode:
        return payload
    tenant = os.environ.get("ZAKURO_TENANT_ID", "").strip() or DEFAULT_TENANT_ID
    if mode == "v2":
        return sign_payload_v2(payload, master_key=read_master_key(), tenant_id=tenant)
    return sign_payload(payload, master_key=read_master_key(), tenant_id=tenant)
