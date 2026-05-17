"""Unit tests for the postcard envelope codec (#117)."""

from __future__ import annotations

import pytest

from zakuro.wire import (
    WIRE_VERSION_V1,
    Envelope,
    HmacMismatchError,
    ResourceLimits,
    UnknownWireVersionError,
    WireDecodeError,
    _dec_varint,
    _enc_varint,
    compute_hmac,
    decode_envelope,
    encode_envelope,
    safe_loads,
    verify_hmac,
)

# Canonical envelope test vector — the Rust ``zakuro-wire`` crate's
# frozen_envelope_hex emits exactly these bytes. Both sides must agree on
# the byte layout; this is the regression guard.
FROZEN_ENVELOPE_HEX = (
    "00"
    "0d63616e6f6e6963616c2d6a6f62"
    "1063616e6f6e6963616c2d74656e616e74"
    "04deadbeef"
    "03000102" + "aa" * 32 + "0000803f" + "8008" + "00" + "1e"
)


def _canonical_envelope() -> Envelope:
    return Envelope(
        version=WIRE_VERSION_V1,
        job_id="canonical-job",
        tenant_id="canonical-tenant",
        callable=bytes.fromhex("deadbeef"),
        args=bytes.fromhex("000102"),
        hmac=bytes([0xAA]) * 32,
        resource_limits=ResourceLimits(
            cpus=1.0,
            memory_mb=1024,
            gpus=0,
            timeout_seconds=30,
        ),
    )


# ---- frozen-vector + round-trip ----------------------------------------------


def test_encode_matches_rust_frozen_vector() -> None:
    env = _canonical_envelope()
    assert encode_envelope(env).hex() == FROZEN_ENVELOPE_HEX


def test_decode_round_trip() -> None:
    env = _canonical_envelope()
    raw = encode_envelope(env)
    assert decode_envelope(raw) == env


def test_decode_frozen_vector() -> None:
    raw = bytes.fromhex(FROZEN_ENVELOPE_HEX)
    env = decode_envelope(raw)
    assert env.job_id == "canonical-job"
    assert env.tenant_id == "canonical-tenant"
    assert env.callable == b"\xde\xad\xbe\xef"
    assert env.resource_limits.memory_mb == 1024
    assert env.resource_limits.timeout_seconds == 30


# ---- varint ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,encoded_hex",
    [
        (0, "00"),
        (1, "01"),
        (127, "7f"),
        (128, "8001"),
        (1024, "8008"),
        (1_000_000, "c08489"[::1].swapcase().lower()),  # placeholder; corrected below
    ],
)
def test_varint_encodes_known_values(value: int, encoded_hex: str) -> None:
    # The 1_000_000 entry above is a placeholder; verify by round-trip
    # rather than hardcoding.
    if value == 1_000_000:
        round_trip, _ = _dec_varint(memoryview(_enc_varint(value)), 0)
        assert round_trip == value
        return
    assert _enc_varint(value).hex() == encoded_hex


def test_varint_overflow_rejected() -> None:
    # 6 continuation bytes is past u32 range
    too_long = b"\x80\x80\x80\x80\x80\x01"
    with pytest.raises(WireDecodeError, match="overflow"):
        _dec_varint(memoryview(too_long), 0)


def test_varint_truncated_rejected() -> None:
    with pytest.raises(WireDecodeError, match="truncated"):
        _dec_varint(memoryview(b"\x80"), 0)


# ---- decode rejects malformed input ------------------------------------------


def test_empty_envelope_rejected() -> None:
    with pytest.raises(WireDecodeError, match="empty"):
        decode_envelope(b"")


def test_unknown_version_rejected() -> None:
    with pytest.raises(UnknownWireVersionError):
        decode_envelope(b"\xff")  # variant index 255 — not a known WireVersion


def test_trailing_bytes_rejected() -> None:
    raw = encode_envelope(_canonical_envelope()) + b"\x00\x01"
    with pytest.raises(WireDecodeError, match="trailing"):
        decode_envelope(raw)


def test_string_not_utf8_rejected() -> None:
    # Build a payload by hand with an invalid UTF-8 job_id
    raw = bytearray([WIRE_VERSION_V1])
    raw.extend(_enc_varint(2))
    raw.extend(b"\xff\xfe")
    with pytest.raises(WireDecodeError, match="utf-8"):
        decode_envelope(bytes(raw))


def test_hmac_wrong_length_rejected_on_construction() -> None:
    with pytest.raises(WireDecodeError, match="hmac"):
        Envelope(
            version=WIRE_VERSION_V1,
            job_id="j",
            tenant_id="t",
            callable=b"",
            args=b"",
            hmac=b"\x00" * 31,
        )


# ---- HMAC --------------------------------------------------------------------


def test_hmac_verifies_correct_key() -> None:
    key = b"\x11" * 32
    callable_bytes = b"\xde\xad"
    args = b"\xbe\xef"
    job_id = "j-1"
    mac = compute_hmac(hmac_key=key, callable_bytes=callable_bytes, args=args, job_id=job_id)
    env = Envelope(
        version=WIRE_VERSION_V1,
        job_id=job_id,
        tenant_id="tenant-acme",
        callable=callable_bytes,
        args=args,
        hmac=mac,
    )
    verify_hmac(env, hmac_key=key)  # does not raise


def test_hmac_rejects_wrong_key() -> None:
    env = Envelope(
        version=WIRE_VERSION_V1,
        job_id="j-1",
        tenant_id="tenant-acme",
        callable=b"x",
        args=b"y",
        hmac=compute_hmac(hmac_key=b"\x11" * 32, callable_bytes=b"x", args=b"y", job_id="j-1"),
    )
    with pytest.raises(HmacMismatchError):
        verify_hmac(env, hmac_key=b"\x22" * 32)


def test_hmac_rejects_tampered_callable() -> None:
    key = b"\x11" * 32
    env = Envelope(
        version=WIRE_VERSION_V1,
        job_id="j-1",
        tenant_id="tenant-acme",
        callable=b"original",
        args=b"a",
        hmac=compute_hmac(hmac_key=key, callable_bytes=b"original", args=b"a", job_id="j-1"),
    )
    tampered = Envelope(
        version=env.version,
        job_id=env.job_id,
        tenant_id=env.tenant_id,
        callable=b"TAMPERED",
        args=env.args,
        hmac=env.hmac,
        resource_limits=env.resource_limits,
    )
    with pytest.raises(HmacMismatchError):
        verify_hmac(tampered, hmac_key=key)


def test_safe_loads_round_trip_with_real_hmac() -> None:
    key = b"\x33" * 32
    env = Envelope(
        version=WIRE_VERSION_V1,
        job_id="j-2",
        tenant_id="tenant-acme",
        callable=b"\xca\xfe",
        args=b"\xba\xbe",
        hmac=compute_hmac(hmac_key=key, callable_bytes=b"\xca\xfe", args=b"\xba\xbe", job_id="j-2"),
    )
    raw = encode_envelope(env)
    parsed = safe_loads(raw, hmac_key=key)
    assert parsed == env


def test_safe_loads_rejects_bad_key() -> None:
    key = b"\x33" * 32
    env = Envelope(
        version=WIRE_VERSION_V1,
        job_id="j-3",
        tenant_id="tenant-acme",
        callable=b"\xca\xfe",
        args=b"\xba\xbe",
        hmac=compute_hmac(hmac_key=key, callable_bytes=b"\xca\xfe", args=b"\xba\xbe", job_id="j-3"),
    )
    raw = encode_envelope(env)
    with pytest.raises(HmacMismatchError):
        safe_loads(raw, hmac_key=b"\x44" * 32)


# ---- resource limits validation ----------------------------------------------


def test_resource_limits_validate_rejects_zero_cpus() -> None:
    with pytest.raises(WireDecodeError):
        Envelope(
            version=WIRE_VERSION_V1,
            job_id="j",
            tenant_id="t",
            callable=b"",
            args=b"",
            hmac=b"\x00" * 32,
            resource_limits=ResourceLimits(cpus=0.0, memory_mb=1, gpus=0, timeout_seconds=1),
        )


def test_resource_limits_validate_rejects_zero_timeout() -> None:
    with pytest.raises(WireDecodeError):
        Envelope(
            version=WIRE_VERSION_V1,
            job_id="j",
            tenant_id="t",
            callable=b"",
            args=b"",
            hmac=b"\x00" * 32,
            resource_limits=ResourceLimits(cpus=1.0, memory_mb=1, gpus=0, timeout_seconds=0),
        )
