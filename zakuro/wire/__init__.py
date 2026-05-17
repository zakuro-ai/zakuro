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
    return _dec_varint_n(buf, offset, max_bytes=5)


def _dec_varint_u64(buf: memoryview, offset: int) -> tuple[int, int]:
    """Decode a postcard varint capped at 10 bytes (u64).

    Postcard uses the same LEB128 layout regardless of width; the cap is
    what changes. v0.2's ``ChunkFrame.stream_id`` is a u64 and needs the
    wider variant.
    """
    return _dec_varint_n(buf, offset, max_bytes=10)


def _dec_varint_n(buf: memoryview, offset: int, *, max_bytes: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for byte_count in range(max_bytes):
        if offset >= len(buf):
            raise WireDecodeError("varint truncated")
        byte = buf[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if byte_count == max_bytes - 1 and (byte & 0x80):
            raise WireDecodeError(f"varint overflow (> {max_bytes} bytes)")
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


# =====================================================================
# v0.2 — state-dict delta encoding (#174) + streaming chunks (#175).
# Separate dataclasses + codec entries so v0.1 byte layout is unchanged.
# Mirrors zakuro_wire::{EnvelopeV2, ChunkFrame, V2Message}.
# =====================================================================

# WireVersion::V2 variant index — postcard encodes it as a single byte.
WIRE_VERSION_V2 = 1


@dataclass(frozen=True)
class EnvelopeV2:
    """v0.2 dispatch envelope.

    Identical to :class:`Envelope` plus two new optional slots:

    * ``cache_key`` — when set, the worker stores the reconstructed
      ``callable`` bytes in its bounded LRU under this key.
    * ``delta_against`` — when set, ``callable`` is a delta against the
      value the worker previously cached under this key. The worker
      reconstructs the full payload before invoking cloudpickle. A
      cache-miss is signalled as a `worker_unavailable` reason — the
      caller retries with the full payload.

    Wire-format invariants
    ----------------------
    * ``version`` MUST be :data:`WIRE_VERSION_V2`.
    * Postcard layout: v0.1 fields first (identical to :class:`Envelope`),
      then ``cache_key`` then ``delta_against``, each as
      ``Option<String>`` (one byte tag 0/1 + length-prefixed UTF-8 when
      present).
    """

    version: int  # WIRE_VERSION_V2
    job_id: str
    tenant_id: str
    callable: bytes
    args: bytes
    hmac: bytes  # 32 bytes
    resource_limits: ResourceLimits = field(
        default_factory=lambda: ResourceLimits(1.0, 1024, 0, 60)
    )
    cache_key: str | None = None
    delta_against: str | None = None

    def __post_init__(self) -> None:
        if self.version != WIRE_VERSION_V2:
            raise UnknownWireVersionError(
                f"EnvelopeV2.version must be V2 ({WIRE_VERSION_V2}), got {self.version}"
            )
        if len(self.hmac) != 32:
            raise WireDecodeError(f"hmac must be 32 bytes, got {len(self.hmac)}")
        if not isinstance(self.callable, (bytes, bytearray)):
            raise WireDecodeError("callable must be bytes")
        if not isinstance(self.args, (bytes, bytearray)):
            raise WireDecodeError("args must be bytes")
        self.resource_limits.validate()


@dataclass(frozen=True)
class ChunkFrame:
    """One frame in a multi-chunk streaming dispatch (#175).

    Logical large payloads (multi-GB state-dicts) are split across
    ``ChunkFrame``s. Each carries the same ``stream_id``, a 0-indexed
    monotonic ``seq``, a ``last`` marker on the final frame, and the
    payload bytes. Concatenation of every ``bytes_`` field in ``seq``
    order yields the postcard-encoded :class:`EnvelopeV2`.
    """

    version: int  # WIRE_VERSION_V2
    stream_id: int
    seq: int
    last: bool
    bytes_: bytes

    def __post_init__(self) -> None:
        if self.version != WIRE_VERSION_V2:
            raise UnknownWireVersionError("ChunkFrame.version must be V2")
        if self.stream_id < 0 or self.stream_id > 0xFFFF_FFFF_FFFF_FFFF:
            raise WireDecodeError(f"stream_id out of range: {self.stream_id}")
        if self.seq < 0 or self.seq > 0xFFFF_FFFF:
            raise WireDecodeError(f"seq out of range: {self.seq}")


# ---- v0.2 codec helpers ------------------------------------------------------


def _append_option_string(out: bytearray, value: str | None) -> None:
    """Postcard Option<String>: 0x00 = None; 0x01 + length-prefixed UTF-8."""
    if value is None:
        out.append(0x00)
        return
    out.append(0x01)
    _append_string(out, value)


def _read_option_string(buf: memoryview, offset: int) -> tuple[str | None, int]:
    if offset >= len(buf):
        raise WireDecodeError("Option<String> tag truncated")
    tag = buf[offset]
    offset += 1
    if tag == 0:
        return None, offset
    if tag != 1:
        raise WireDecodeError(f"unexpected Option<String> tag: {tag}")
    return _read_string(buf, offset)


def encode_envelope_v2(env: EnvelopeV2) -> bytes:
    """Serialise an :class:`EnvelopeV2` into postcard bytes."""
    if env.version != WIRE_VERSION_V2:
        raise WireDecodeError(f"unsupported wire version: {env.version}")
    out = bytearray()
    out.append(WIRE_VERSION_V2)
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
    _append_option_string(out, env.cache_key)
    _append_option_string(out, env.delta_against)
    return bytes(out)


def decode_envelope_v2(raw: bytes) -> EnvelopeV2:
    """Parse postcard bytes into an :class:`EnvelopeV2`. Raises on malformed input."""
    buf = memoryview(raw)
    if not buf:
        raise WireDecodeError("empty envelope")
    version = buf[0]
    if version != WIRE_VERSION_V2:
        raise UnknownWireVersionError(f"expected V2 envelope; got version {version}")
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
    cache_key, offset = _read_option_string(buf, offset)
    delta_against, offset = _read_option_string(buf, offset)
    if offset != len(buf):
        raise WireDecodeError(f"trailing bytes after EnvelopeV2: {len(buf) - offset}")
    return EnvelopeV2(
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
        cache_key=cache_key,
        delta_against=delta_against,
    )


def encode_chunk_frame(c: ChunkFrame) -> bytes:
    """Serialise a :class:`ChunkFrame` into postcard bytes."""
    if c.version != WIRE_VERSION_V2:
        raise WireDecodeError(f"unsupported wire version: {c.version}")
    out = bytearray()
    out.append(WIRE_VERSION_V2)
    out.extend(_enc_varint(c.stream_id))
    out.extend(_enc_varint(c.seq))
    out.append(0x01 if c.last else 0x00)
    _append_bytes(out, c.bytes_)
    return bytes(out)


def decode_chunk_frame(raw: bytes) -> ChunkFrame:
    """Parse postcard bytes into a :class:`ChunkFrame`."""
    buf = memoryview(raw)
    if not buf:
        raise WireDecodeError("empty chunk frame")
    version = buf[0]
    if version != WIRE_VERSION_V2:
        raise UnknownWireVersionError(f"expected V2 chunk; got version {version}")
    offset = 1
    stream_id, offset = _dec_varint_u64(buf, offset)
    seq, offset = _dec_varint(buf, offset)
    if offset >= len(buf):
        raise WireDecodeError("last-flag truncated")
    last = buf[offset]
    offset += 1
    if last not in (0, 1):
        raise WireDecodeError(f"invalid last-flag: {last}")
    payload, offset = _read_bytes(buf, offset)
    if offset != len(buf):
        raise WireDecodeError(f"trailing bytes after ChunkFrame: {len(buf) - offset}")
    return ChunkFrame(
        version=version,
        stream_id=int(stream_id),
        seq=int(seq),
        last=bool(last),
        bytes_=payload,
    )


__all__ = [
    "ChunkFrame",
    "Envelope",
    "EnvelopeV2",
    "HmacMismatchError",
    "ResourceLimits",
    "UnknownWireVersionError",
    "WIRE_VERSION_V1",
    "WIRE_VERSION_V2",
    "WireDecodeError",
    "WireError",
    "compute_hmac",
    "decode_chunk_frame",
    "decode_envelope",
    "decode_envelope_v2",
    "encode_chunk_frame",
    "encode_envelope",
    "encode_envelope_v2",
    "safe_loads",
    "verify_hmac",
]


# Module-level sanity: keep mypy happy with the typing dance above.
_ = Any  # noqa: F841 -- Any imported only for forward-compat type hints
