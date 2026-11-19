"""ChunkFrame wire codec (#175), ported from the public package's tests/wire/test_wire_v2.py.

Only the ChunkFrame half is ported: this repo's ``EnvelopeV2`` is the ZAK-0020
signed envelope (``issued_at_ms`` + framed MAC), not the public cache/delta one.
"""

from __future__ import annotations

import pytest

from zakuro.wire import (
    WIRE_VERSION_V2,
    ChunkFrame,
    UnknownWireVersionError,
    WireDecodeError,
    decode_chunk_frame,
    encode_chunk_frame,
)


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
