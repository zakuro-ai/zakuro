"""Tests for the v0.2 wire surface (#174 EnvelopeV2 + #175 ChunkFrame)."""

from __future__ import annotations

import pytest

from zakuro.wire import (
    WIRE_VERSION_V2,
    ChunkFrame,
    EnvelopeV2,
    ResourceLimits,
    UnknownWireVersionError,
    WireDecodeError,
    decode_chunk_frame,
    decode_envelope_v2,
    encode_chunk_frame,
    encode_envelope_v2,
)


def _canonical_v2(*, cache_key: str | None = None, delta_against: str | None = None) -> EnvelopeV2:
    return EnvelopeV2(
        version=WIRE_VERSION_V2,
        job_id="canonical-job",
        tenant_id="canonical-tenant",
        callable=bytes.fromhex("deadbeef"),
        args=bytes.fromhex("000102"),
        hmac=bytes([0xAA]) * 32,
        resource_limits=ResourceLimits(cpus=1.0, memory_mb=1024, gpus=0, timeout_seconds=30),
        cache_key=cache_key,
        delta_against=delta_against,
    )


# ---- EnvelopeV2 -------------------------------------------------------------


def test_envelope_v2_round_trip_with_delta_fields() -> None:
    env = _canonical_v2(cache_key="epoch-1", delta_against="epoch-0")
    raw = encode_envelope_v2(env)
    back = decode_envelope_v2(raw)
    assert back == env


def test_envelope_v2_round_trip_without_delta_fields() -> None:
    env = _canonical_v2()
    raw = encode_envelope_v2(env)
    back = decode_envelope_v2(raw)
    assert back == env
    assert back.cache_key is None
    assert back.delta_against is None


def test_envelope_v2_none_encoding_smaller_than_some() -> None:
    """None fields cost 1 byte each; Some fields cost 1 + varint(len) + len."""
    with_keys = encode_envelope_v2(_canonical_v2(cache_key="abc", delta_against="def"))
    without_keys = encode_envelope_v2(_canonical_v2())
    assert len(without_keys) < len(with_keys)
    assert len(with_keys) - len(without_keys) == (1 + 1 + 3) - 1 + (1 + 1 + 3) - 1


def test_envelope_v2_first_byte_is_v2_tag() -> None:
    raw = encode_envelope_v2(_canonical_v2())
    assert raw[0] == WIRE_VERSION_V2 == 1


def test_envelope_v2_rejects_v1_version_tag() -> None:
    with pytest.raises(UnknownWireVersionError):
        EnvelopeV2(
            version=0,  # V1
            job_id="j",
            tenant_id="t",
            callable=b"",
            args=b"",
            hmac=b"\x00" * 32,
        )


def test_envelope_v2_decode_rejects_v1_bytes() -> None:
    """Bytes that start with V1 (0x00) must not be parsed as V2 envelopes."""
    v1_like = b"\x00" + b"\x01a\x01b" + b"\x00" * 50
    with pytest.raises(UnknownWireVersionError):
        decode_envelope_v2(v1_like)


def test_envelope_v2_trailing_bytes_rejected() -> None:
    raw = encode_envelope_v2(_canonical_v2()) + b"\x00"
    with pytest.raises(WireDecodeError, match="trailing"):
        decode_envelope_v2(raw)


def test_envelope_v2_unknown_option_tag_rejected() -> None:
    raw = encode_envelope_v2(_canonical_v2())
    tampered = bytearray(raw)
    # Find the cache_key Option tag (should be the second-to-last region)
    # and corrupt the first option byte to an invalid tag.
    # The encoding ends with: <cache_key tag><cache_key payload?><delta tag>...
    # For a Both-None envelope the last two bytes are 0x00 0x00. Flip the
    # first to 0x02 (invalid) and expect rejection.
    tampered[-2] = 0x02
    with pytest.raises(WireDecodeError, match="Option<String> tag"):
        decode_envelope_v2(bytes(tampered))


# ---- ChunkFrame --------------------------------------------------------------


def test_chunk_frame_round_trip() -> None:
    c = ChunkFrame(
        version=WIRE_VERSION_V2,
        stream_id=0xDEADBEEF,
        seq=7,
        last=False,
        bytes_=b"\x10\x20\x30\x40\x50",
    )
    raw = encode_chunk_frame(c)
    back = decode_chunk_frame(raw)
    assert back == c


def test_chunk_frame_last_marker_distinguishes_bytes() -> None:
    c0 = ChunkFrame(version=WIRE_VERSION_V2, stream_id=1, seq=0, last=False, bytes_=b"\xaa")
    c1 = ChunkFrame(version=WIRE_VERSION_V2, stream_id=1, seq=0, last=True, bytes_=b"\xaa")
    assert encode_chunk_frame(c0) != encode_chunk_frame(c1)


def test_chunk_frame_rejects_invalid_last_byte() -> None:
    raw = encode_chunk_frame(
        ChunkFrame(version=WIRE_VERSION_V2, stream_id=1, seq=0, last=False, bytes_=b"")
    )
    tampered = bytearray(raw)
    # The `last` flag is exactly after stream_id (varint) + seq (varint).
    # For stream_id=1 (single byte) + seq=0 (single byte), it sits at
    # offset 1 + 1 + 1 = 3.
    tampered[3] = 0x02
    with pytest.raises(WireDecodeError, match="last-flag"):
        decode_chunk_frame(bytes(tampered))


def test_chunk_frame_large_stream_id_is_u64() -> None:
    """stream_id must accept full u64 range — varint can be up to 10 bytes."""
    c = ChunkFrame(
        version=WIRE_VERSION_V2,
        stream_id=2**63,  # well above u32
        seq=0,
        last=True,
        bytes_=b"",
    )
    raw = encode_chunk_frame(c)
    back = decode_chunk_frame(raw)
    assert back.stream_id == 2**63


def test_chunk_frame_rejects_v1_version_tag() -> None:
    v1_like = b"\x00\x01\x00\x00\x00"
    with pytest.raises(UnknownWireVersionError):
        decode_chunk_frame(v1_like)


def test_chunk_frame_round_trip_stream_assembly() -> None:
    """Concatenating bytes_ in seq order reconstructs the original payload."""
    payload = b"the quick brown fox jumps over the lazy dog" * 20
    chunks = []
    for i in range(0, len(payload), 100):
        slice_ = payload[i : i + 100]
        chunks.append(
            ChunkFrame(
                version=WIRE_VERSION_V2,
                stream_id=42,
                seq=i // 100,
                last=(i + 100 >= len(payload)),
                bytes_=slice_,
            )
        )
    # Encode + decode each and re-assemble
    decoded = [decode_chunk_frame(encode_chunk_frame(c)) for c in chunks]
    decoded.sort(key=lambda c: c.seq)
    assert b"".join(c.bytes_ for c in decoded) == payload
    assert decoded[-1].last is True
    assert all(not c.last for c in decoded[:-1])
