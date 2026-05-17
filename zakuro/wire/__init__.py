"""Postcard wire format for Zakuro dispatch envelopes (closes #117).

This module is the Python-side mirror of the Rust ``zakuro-wire`` crate.
It exists so the worker can decode dispatches that the broker sent
postcard-encoded — replacing the historical ``cloudpickle.loads(raw)``
call site, where a crafted byte stream went straight to cloudpickle
unbalanced by any signature or schema check.

Trust boundary moved by this module
-----------------------------------

Before: any caller who could put bytes on the wire could trigger
        ``cloudpickle.loads(...)`` inside the worker. The first line of
        defence was network-layer auth, but if that bypassed once,
        cloudpickle gadgets ran in-process.

After:  the worker calls :func:`safe_loads` which:

        1. ``postcard.from_bytes(raw) -> Envelope`` — fixed schema, no
           arbitrary types. Postcard refuses unknown variants, length
           overflows, trailing bytes.
        2. HMAC-SHA-256 check over ``callable || args || job_id`` with
           the per-tenant key. Mismatch → :class:`HmacMismatchError`,
           bytes never reach cloudpickle.
        3. Only after both succeed does the worker call
           ``cloudpickle.loads(envelope.callable)``.

The HMAC-key derivation is documented in
`RFC 0002 § HMAC key <https://github.com/zakuro-ai/zakuro/blob/master/docs/rfcs/0002-auth-mtls-jwt.md>`_;
this module accepts the derived 32-byte key from the caller — key
derivation itself is the auth module's job (#116).

What this module is *not*
-------------------------

* Not a general-purpose postcard codec. It implements exactly the
  fields the v1 :class:`Envelope` and :class:`ExecutionResult` carry.
  Other schemas would need their own encode/decode paths (intentionally
  — keep the attack surface small).
* Not a key-management helper. ``hmac_key`` is supplied by the caller.

Wire-compatibility test vector
------------------------------

The Rust ``zakuro-wire`` crate test ``frozen_envelope_hex`` produces a
known byte sequence that this module's :func:`encode_envelope` is
required to reproduce. See ``tests/wire/test_frozen_vector.py``.
"""

from __future__ import annotations

import hmac as _hmac
import struct
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


class WireError(Exception):
    """Base for wire-format failures (decode, schema, HMAC)."""


class WireDecodeError(WireError):
    """The bytes did not decode into a valid v1 envelope."""


class UnknownWireVersionError(WireDecodeError):
    """The top-level WireVersion byte was not a known variant."""


class HmacMismatchError(WireError):
    """HMAC over the envelope payload did not match the expected key."""


# Top-level WireVersion enum mirrors the Rust crate.
WIRE_VERSION_V1 = 0


@dataclass(frozen=True)
class ResourceLimits:
    """Per-job resource budget mirrored from ``zakuro_wire::ResourceLimits``."""

    cpus: float
    memory_mb: int
    gpus: int
    timeout_seconds: int

    def validate(self) -> None:
        if not (0.0 < self.cpus <= 1024.0):
            raise WireDecodeError(f"cpus out of range: {self.cpus}")
        if not (0 <= self.memory_mb <= 1024 * 1024):
            raise WireDecodeError(f"memory_mb out of range: {self.memory_mb}")
        if not (0 <= self.gpus <= 256):
            raise WireDecodeError(f"gpus out of range: {self.gpus}")
        if not (0 < self.timeout_seconds <= 24 * 3600):
            raise WireDecodeError(f"timeout_seconds out of range: {self.timeout_seconds}")


@dataclass(frozen=True)
class Envelope:
    """Dispatch envelope mirrored from ``zakuro_wire::Envelope`` (v1)."""

    version: int  # WIRE_VERSION_V1
    job_id: str
    tenant_id: str
    callable: bytes
    args: bytes
    hmac: bytes  # 32 bytes
    resource_limits: ResourceLimits = field(
        default_factory=lambda: ResourceLimits(1.0, 1024, 0, 60)
    )

    def __post_init__(self) -> None:
        if self.version != WIRE_VERSION_V1:
            raise UnknownWireVersionError(f"unsupported wire version: {self.version}")
        if len(self.hmac) != 32:
            raise WireDecodeError(f"hmac must be 32 bytes, got {len(self.hmac)}")
        if not isinstance(self.callable, (bytes, bytearray)):
            raise WireDecodeError("callable must be bytes")
        if not isinstance(self.args, (bytes, bytearray)):
            raise WireDecodeError("args must be bytes")
        self.resource_limits.validate()


# ---- postcard varint (unsigned) ----------------------------------------------


def _enc_varint(value: int) -> bytes:
    """Encode an unsigned integer as a postcard variable-length integer."""
    if value < 0:
        raise WireDecodeError(f"negative varint: {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _dec_varint(buf: memoryview, offset: int) -> tuple[int, int]:
    """Decode a postcard varint; return (value, new_offset). Caps at 5 bytes (u32)."""
    value = 0
    shift = 0
    for byte_count in range(5):
        if offset >= len(buf):
            raise WireDecodeError("varint truncated")
        byte = buf[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if byte_count == 4 and (byte & 0x80):
            raise WireDecodeError("varint overflow (> 5 bytes)")
    raise WireDecodeError("varint did not terminate")


# ---- envelope codec -----------------------------------------------------------


def encode_envelope(env: Envelope) -> bytes:
    """Serialise *env* into postcard-encoded bytes byte-compatible with the Rust crate."""
    if env.version != WIRE_VERSION_V1:
        raise WireDecodeError(f"unsupported wire version: {env.version}")
    out = bytearray()
    # WireVersion::V1 → variant index 0 → single byte 0x00
    out.append(WIRE_VERSION_V1)
    _append_string(out, env.job_id)
    _append_string(out, env.tenant_id)
    _append_bytes(out, env.callable)
    _append_bytes(out, env.args)
    if len(env.hmac) != 32:
        raise WireDecodeError("hmac must be 32 bytes")
    out.extend(env.hmac)
    out.extend(struct.pack("<f", env.resource_limits.cpus))
    out.extend(_enc_varint(env.resource_limits.memory_mb))
    out.extend(_enc_varint(env.resource_limits.gpus))
    out.extend(_enc_varint(env.resource_limits.timeout_seconds))
    return bytes(out)


def decode_envelope(raw: bytes) -> Envelope:
    """Parse postcard-encoded bytes into an :class:`Envelope`. Raises on malformed input."""
    buf = memoryview(raw)
    if not buf:
        raise WireDecodeError("empty envelope")
    version = buf[0]
    if version != WIRE_VERSION_V1:
        raise UnknownWireVersionError(f"unknown wire version: {version}")
    offset = 1
    job_id, offset = _read_string(buf, offset)
    tenant_id, offset = _read_string(buf, offset)
    callable_bytes, offset = _read_bytes(buf, offset)
    args, offset = _read_bytes(buf, offset)
    if offset + 32 > len(buf):
        raise WireDecodeError("hmac truncated")
    hmac_bytes = bytes(buf[offset : offset + 32])
    offset += 32
    if offset + 4 > len(buf):
        raise WireDecodeError("cpus (f32) truncated")
    cpus = struct.unpack_from("<f", buf, offset)[0]
    offset += 4
    memory_mb, offset = _dec_varint(buf, offset)
    gpus, offset = _dec_varint(buf, offset)
    timeout_seconds, offset = _dec_varint(buf, offset)
    if offset != len(buf):
        raise WireDecodeError(f"trailing bytes after envelope: {len(buf) - offset}")
    return Envelope(
        version=version,
        job_id=job_id,
        tenant_id=tenant_id,
        callable=callable_bytes,
        args=args,
        hmac=hmac_bytes,
        resource_limits=ResourceLimits(
            cpus=float(cpus),
            memory_mb=int(memory_mb),
            gpus=int(gpus),
            timeout_seconds=int(timeout_seconds),
        ),
    )


def _append_string(out: bytearray, value: str) -> None:
    data = value.encode("utf-8")
    out.extend(_enc_varint(len(data)))
    out.extend(data)


def _append_bytes(out: bytearray, value: bytes) -> None:
    out.extend(_enc_varint(len(value)))
    out.extend(value)


def _read_string(buf: memoryview, offset: int) -> tuple[str, int]:
    length, offset = _dec_varint(buf, offset)
    if offset + length > len(buf):
        raise WireDecodeError("string truncated")
    try:
        s = bytes(buf[offset : offset + length]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WireDecodeError(f"string is not valid utf-8: {exc}") from exc
    return s, offset + length


def _read_bytes(buf: memoryview, offset: int) -> tuple[bytes, int]:
    length, offset = _dec_varint(buf, offset)
    if offset + length > len(buf):
        raise WireDecodeError("byte field truncated")
    return bytes(buf[offset : offset + length]), offset + length


# ---- HMAC ---------------------------------------------------------------------


def compute_hmac(*, hmac_key: bytes, callable_bytes: bytes, args: bytes, job_id: str) -> bytes:
    """Return HMAC-SHA-256 over ``callable || args || job_id``.

    Matches the Rust crate's documented HMAC layout. The per-tenant key is
    derived elsewhere (RFC 0002 §"HMAC key"); this module only consumes it.
    """
    mac = _hmac.new(hmac_key, digestmod=sha256)
    mac.update(callable_bytes)
    mac.update(args)
    mac.update(job_id.encode("utf-8"))
    return mac.digest()


def verify_hmac(env: Envelope, *, hmac_key: bytes) -> None:
    """Raise :class:`HmacMismatchError` if *env.hmac* doesn't match the key.

    Constant-time comparison.
    """
    expected = compute_hmac(
        hmac_key=hmac_key,
        callable_bytes=env.callable,
        args=env.args,
        job_id=env.job_id,
    )
    if not _hmac.compare_digest(expected, env.hmac):
        raise HmacMismatchError("envelope HMAC did not match")


def safe_loads(raw: bytes, *, hmac_key: bytes) -> Envelope:
    """Decode + HMAC-verify a dispatch envelope.

    This is the call site the worker should use in place of any
    ``cloudpickle.loads(raw)`` that happens before authorisation has
    inspected the payload. Returns the parsed :class:`Envelope`; the
    caller is then free to ``cloudpickle.loads(envelope.callable)``
    knowing the bytes carry an HMAC checked against the tenant's key.

    Raises :class:`WireDecodeError`, :class:`UnknownWireVersionError`,
    or :class:`HmacMismatchError` — all subclasses of :class:`WireError`.
    """
    env = decode_envelope(raw)
    verify_hmac(env, hmac_key=hmac_key)
    return env


__all__ = [
    "Envelope",
    "HmacMismatchError",
    "ResourceLimits",
    "UnknownWireVersionError",
    "WIRE_VERSION_V1",
    "WireDecodeError",
    "WireError",
    "compute_hmac",
    "decode_envelope",
    "encode_envelope",
    "safe_loads",
    "verify_hmac",
]


# Module-level sanity: keep mypy happy with the typing dance above.
_ = Any  # noqa: F841 -- Any imported only for forward-compat type hints
