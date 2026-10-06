"""Postcard wire format for the signed ``/execute`` envelope (ZAKURO_WIRE=v1).

Port of the v1 subset of ``develop``'s ``zakuro.wire`` (ADR ZAK-0013). The
worker decodes a fixed-schema envelope and verifies an HMAC-SHA-256 over
``callable || args || job_id`` *before* any byte reaches ``cloudpickle.loads``.
Only the v1 envelope is ported here; the v2 delta/streaming codecs are not.

Key derivation lives in :mod:`zakuro.wire.keys`; signing helpers for clients in
:func:`zakuro.wire.keys.wrap_payload`.
"""

from __future__ import annotations

import hmac as _hmac
import struct
from dataclasses import dataclass, field
from hashlib import sha256


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
WIRE_VERSION_V2 = 1  # ZAK-0020: length-framed, domain-separated MAC + issued-at
V2_MAC_DOMAIN = b"zakuro-wire-v2"


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


# ---- envelope v2 (ZAK-0020) ---------------------------------------------------


@dataclass(frozen=True)
class EnvelopeV2:
    """Dispatch envelope v2: like v1 plus ``issued_at_ms`` (u64) and a framed MAC."""

    job_id: str
    tenant_id: str
    issued_at_ms: int
    callable: bytes
    args: bytes
    hmac: bytes  # 32 bytes
    resource_limits: ResourceLimits = field(
        default_factory=lambda: ResourceLimits(1.0, 1024, 0, 60)
    )
    version: int = WIRE_VERSION_V2

    def __post_init__(self) -> None:
        if self.version != WIRE_VERSION_V2:
            raise UnknownWireVersionError(f"unsupported wire version: {self.version}")
        if len(self.hmac) != 32:
            raise WireDecodeError(f"hmac must be 32 bytes, got {len(self.hmac)}")
        if not 0 <= self.issued_at_ms < 1 << 64:
            raise WireDecodeError("issued_at_ms out of u64 range")
        self.resource_limits.validate()


def peek_version(raw: bytes) -> int:
    """Return the leading WireVersion byte of *raw* (raises on empty input)."""
    if not raw:
        raise WireDecodeError("empty envelope")
    return raw[0]


def encode_envelope_v2(env: EnvelopeV2) -> bytes:
    """Serialise a v2 envelope: v1 layout with ``issued_at_ms`` (varint u64) after tenant_id."""
    out = bytearray([WIRE_VERSION_V2])
    _append_string(out, env.job_id)
    _append_string(out, env.tenant_id)
    out.extend(_enc_varint(env.issued_at_ms))
    _append_bytes(out, env.callable)
    _append_bytes(out, env.args)
    out.extend(env.hmac)
    out.extend(struct.pack("<f", env.resource_limits.cpus))
    out.extend(_enc_varint(env.resource_limits.memory_mb))
    out.extend(_enc_varint(env.resource_limits.gpus))
    out.extend(_enc_varint(env.resource_limits.timeout_seconds))
    return bytes(out)


def decode_envelope_v2(raw: bytes) -> EnvelopeV2:
    """Parse a v2 envelope. Raises on a wrong version byte or malformed input."""
    buf = memoryview(raw)
    if peek_version(raw) != WIRE_VERSION_V2:
        raise UnknownWireVersionError(f"not a v2 envelope: version {buf[0]}")
    offset = 1
    job_id, offset = _read_string(buf, offset)
    tenant_id, offset = _read_string(buf, offset)
    issued_at_ms, offset = _dec_varint_u64(buf, offset)
    callable_bytes, offset = _read_bytes(buf, offset)
    args, offset = _read_bytes(buf, offset)
    if offset + 32 + 4 > len(buf):
        raise WireDecodeError("hmac or limits truncated")
    hmac_bytes = bytes(buf[offset : offset + 32])
    offset += 32
    cpus = struct.unpack_from("<f", buf, offset)[0]
    offset += 4
    memory_mb, offset = _dec_varint(buf, offset)
    gpus, offset = _dec_varint(buf, offset)
    timeout_seconds, offset = _dec_varint(buf, offset)
    if offset != len(buf):
        raise WireDecodeError(f"trailing bytes after envelope: {len(buf) - offset}")
    return EnvelopeV2(
        job_id=job_id,
        tenant_id=tenant_id,
        issued_at_ms=issued_at_ms,
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


def _frame(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def compute_hmac_v2(
    *,
    hmac_key: bytes,
    tenant_id: str,
    job_id: str,
    issued_at_ms: int,
    callable_bytes: bytes,
    args: bytes,
) -> bytes:
    """HMAC-SHA-256 over ``"zakuro-wire-v2"`` then each field as u32-BE length + bytes.

    Field order: version (1 byte), tenant_id, job_id, issued_at_ms (8 bytes BE),
    callable, args. Length framing means bytes cannot be shifted between fields.
    """
    mac = _hmac.new(hmac_key, digestmod=sha256)
    mac.update(V2_MAC_DOMAIN)
    for part in (
        bytes([WIRE_VERSION_V2]),
        tenant_id.encode("utf-8"),
        job_id.encode("utf-8"),
        struct.pack(">Q", issued_at_ms),
        bytes(callable_bytes),
        bytes(args),
    ):
        mac.update(_frame(part))
    return mac.digest()


def verify_hmac_v2(env: EnvelopeV2, *, hmac_key: bytes) -> None:
    """Raise :class:`HmacMismatchError` if the v2 MAC does not match (constant time)."""
    expected = compute_hmac_v2(
        hmac_key=hmac_key,
        tenant_id=env.tenant_id,
        job_id=env.job_id,
        issued_at_ms=env.issued_at_ms,
        callable_bytes=env.callable,
        args=env.args,
    )
    if not _hmac.compare_digest(expected, env.hmac):
        raise HmacMismatchError("envelope HMAC did not match")
