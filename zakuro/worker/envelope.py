"""Envelope-unwrap helper for the worker `/execute` boundary (#117 Phase 2).

This module is the single place where ``cloudpickle.loads`` for inbound
requests is gated by a postcard-envelope HMAC verification. The worker's
``/execute`` handler (and the QUIC equivalent) call :func:`unwrap_payload`
on the raw bytes; the function returns the *inner* cloudpickle blob that
``zakuro.worker.executor.execute_function`` knows how to handle.

Rollout switch (``ZAKURO_WIRE``)
-------------------------------

* ``ZAKURO_WIRE=v1`` — strict mode. Every request body is decoded as a
  postcard envelope; HMAC verified against the per-tenant key derived
  from the worker's master key (``ZAKURO_HMAC_KEY``). On failure: raise
  :class:`zakuro.wire.WireError`, the handler turns that into a 401.
* unset (default) — legacy mode. The raw bytes are passed through to
  ``execute_function`` unchanged, exactly as today. This is the v0.3→v0.4
  rollout switch from RFC 0001 §"Step 5 — flip strict".

Multi-tenant key derivation
---------------------------

* The master key is read from ``ZAKURO_HMAC_KEY`` (hex) or
  ``ZAKURO_HMAC_KEY_FILE`` (raw bytes). Either-or; setting both raises.
* The per-tenant verification key is
  ``derive_payload_hmac_key(master, envelope.tenant_id)``. Each tenant
  gets a distinct HMAC key without the worker holding per-tenant
  secrets — the broker computes the same derivation and signs the
  envelope with the tenant-specific key.
* The worker never sees the JWT signing key directly. The master key
  ``ZAKURO_HMAC_KEY`` is a separate per-mesh secret distributed via
  SOPS+age (RFC 0004 / zakuro-image #15).
"""

from __future__ import annotations

import os
from pathlib import Path

from zakuro.auth.jwt import derive_payload_hmac_key
from zakuro.wire import Envelope, WireError


class EnvelopeRejectedError(WireError):
    """Raised when the envelope is invalid or fails HMAC.

    The handler should turn this into HTTP 401 with no body — leaking
    *why* would aid enumeration.
    """


# Back-compat alias for code (and tests) that imported the original
# name before the ruff N818 rename.
EnvelopeRejected = EnvelopeRejectedError


def is_wire_strict() -> bool:
    """Return True iff ``ZAKURO_WIRE=v1`` — postcard envelope is required."""
    return os.environ.get("ZAKURO_WIRE", "").strip().lower() == "v1"


def _read_master_key() -> bytes:
    """Read the per-mesh HMAC master key from env. Fail closed."""
    hex_key = os.environ.get("ZAKURO_HMAC_KEY", "").strip()
    key_file = os.environ.get("ZAKURO_HMAC_KEY_FILE", "").strip()
    if hex_key and key_file:
        raise EnvelopeRejectedError(
            "Both ZAKURO_HMAC_KEY and ZAKURO_HMAC_KEY_FILE are set — pick one."
        )
    if hex_key:
        try:
            return bytes.fromhex(hex_key)
        except ValueError as exc:
            raise EnvelopeRejectedError(f"ZAKURO_HMAC_KEY is not valid hex: {exc}") from exc
    if key_file:
        try:
            return Path(key_file).expanduser().read_bytes().strip()
        except OSError as exc:
            raise EnvelopeRejectedError(f"could not read ZAKURO_HMAC_KEY_FILE: {exc}") from exc
    raise EnvelopeRejectedError(
        "ZAKURO_WIRE=v1 but no HMAC master key configured — set ZAKURO_HMAC_KEY or "
        "ZAKURO_HMAC_KEY_FILE."
    )


def unwrap_payload(raw: bytes) -> tuple[bytes, Envelope | None]:
    """Return ``(inner_payload, envelope_or_None)`` for the executor.

    When :func:`is_wire_strict` is true, *raw* is decoded as a postcard
    envelope, HMAC-verified against the derived per-tenant key, and the
    envelope's ``callable`` blob is returned as ``inner_payload``.

    When wire-strict mode is off, *raw* is returned unchanged and the
    envelope tuple slot is ``None`` — the legacy path is preserved
    exactly as it was before #117 Phase 2.

    Raises :class:`EnvelopeRejected` (a :class:`WireError`) on any
    decode / HMAC / config failure.
    """
    from zakuro.worker.posture import StartupConfigError, max_payload_bytes

    try:
        cap = max_payload_bytes()
    except StartupConfigError:  # malformed cap at request time — fail closed
        cap = 256 * 1024 * 1024
    if len(raw) > cap:
        raise EnvelopeRejectedError(
            f"request body {len(raw)} bytes exceeds ZAKURO_MAX_PAYLOAD_BYTES ({cap})"
        )

    if not is_wire_strict():
        return raw, None

    master_key = _read_master_key()
    try:
        # Decode first so we can derive the per-tenant key from the
        # envelope's tenant_id. The HMAC check is then performed against
        # the derived key — see safe_loads' two-step contract.
        from zakuro.wire import decode_envelope, verify_hmac

        envelope = decode_envelope(raw)
        tenant_key = derive_payload_hmac_key(
            jwt_signing_key=master_key,
            tenant_id=envelope.tenant_id,
        )
        verify_hmac(envelope, hmac_key=tenant_key)
    except WireError:
        raise
    except Exception as exc:  # pragma: no cover - hardening
        raise EnvelopeRejectedError(f"envelope rejected: {exc}") from exc

    return envelope.callable, envelope


# Helper used by the matching unit test — encode an envelope that the
# strict path will accept. Lives here so callers don't have to import
# the derivation chain themselves.
def build_envelope_payload(
    *,
    callable_bytes: bytes,
    args: bytes = b"",
    job_id: str = "j-1",
    tenant_id: str = "tenant-acme",
    master_key: bytes,
) -> bytes:
    """Encode + sign a postcard envelope ready to POST to /execute.

    Mirror of what a well-behaved client would do — used by tests and
    by ``scripts/`` smoke tools. Production clients build this via the
    forthcoming ``zakuro.client.envelope`` helper.
    """
    from zakuro.wire import (
        WIRE_VERSION_V1,
        ResourceLimits,
        compute_hmac,
        encode_envelope,
    )

    tenant_key = derive_payload_hmac_key(jwt_signing_key=master_key, tenant_id=tenant_id)
    mac = compute_hmac(hmac_key=tenant_key, callable_bytes=callable_bytes, args=args, job_id=job_id)
    envelope = Envelope(
        version=WIRE_VERSION_V1,
        job_id=job_id,
        tenant_id=tenant_id,
        callable=callable_bytes,
        args=args,
        hmac=mac,
        resource_limits=ResourceLimits(cpus=1.0, memory_mb=1024, gpus=0, timeout_seconds=60),
    )
    return encode_envelope(envelope)
