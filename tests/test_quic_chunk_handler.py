"""QUIC multi-chunk dispatch (OP_EXECUTE_CHUNK, #175).

The reassembled stream is handled exactly like an OP_EXECUTE payload, so it
goes through the same ``unwrap_payload`` gate: legacy cloudpickle with
``ZAKURO_WIRE`` unset, a verified signed envelope under ``ZAKURO_WIRE=v1|v2``.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import cloudpickle
import pytest

from zakuro.wire import WIRE_VERSION_V2, ChunkFrame, encode_chunk_frame

pytest.importorskip("aioquic")

from zakuro.worker.quic_server import (  # noqa: E402  -- aioquic must be importable first
    STAT_CHUNK_ACK,
    STAT_OK,
    STAT_PROTOCOL_ERROR,
    WorkerQuicProtocol,
)


@pytest.fixture(autouse=True)
def _legacy_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("ZAKURO_WIRE", "ZAKURO_HMAC_KEY", "ZAKURO_HMAC_KEY_FILE", "ZAKURO_MAX_PAYLOAD_BYTES"):
        monkeypatch.delenv(k, raising=False)


def _protocol() -> WorkerQuicProtocol:
    """A WorkerQuicProtocol without aioquic's connection plumbing (only _handle_chunk is used)."""
    from zakuro.wire.streaming import ChunkReassembler

    proto = WorkerQuicProtocol.__new__(WorkerQuicProtocol)
    proto._executor = ThreadPoolExecutor(max_workers=1)
    proto._reassembler = ChunkReassembler()
    return proto


def _chunks(payload: bytes, *, stream_id: int = 7, size: int = 64) -> list[bytes]:
    parts = [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]
    return [
        encode_chunk_frame(
            ChunkFrame(
                version=WIRE_VERSION_V2,
                stream_id=stream_id,
                seq=i,
                last=i == len(parts) - 1,
                bytes_=part,
            )
        )
        for i, part in enumerate(parts)
    ]


def _run(proto: WorkerQuicProtocol, frame: bytes) -> tuple[int, bytes]:
    return asyncio.run(proto._handle_chunk(frame))


def _square(x: int) -> int:
    return x * x


def test_chunked_dispatch_round_trips() -> None:
    proto = _protocol()
    frames = _chunks(cloudpickle.dumps({"func": _square, "args": (12,)}))
    assert len(frames) > 1
    for frame in frames[:-1]:
        assert _run(proto, frame) == (STAT_CHUNK_ACK, b"")
    status, body = _run(proto, frames[-1])
    assert status == STAT_OK
    assert cloudpickle.loads(body) == 144


def test_malformed_and_out_of_order_chunks_rejected() -> None:
    proto = _protocol()
    assert _run(proto, b"\x00garbage")[0] == STAT_PROTOCOL_ERROR
    frames = _chunks(b"x" * 200)
    assert _run(proto, frames[1])[0] == STAT_PROTOCOL_ERROR  # seq 1 before seq 0


def test_reassembled_stream_goes_through_the_envelope_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ZAKURO_WIRE an unsigned reassembled stream is refused, never unpickled."""
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", "11" * 32)
    proto = _protocol()
    frames = _chunks(cloudpickle.dumps({"func": _square, "args": (3,)}))
    for frame in frames[:-1]:
        _run(proto, frame)
    status, body = _run(proto, frames[-1])
    assert status == STAT_PROTOCOL_ERROR
    assert b"unauthorized" in body


def test_signed_reassembled_stream_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    from zakuro.wire.keys import sign_payload

    key = b"\x11" * 32
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", key.hex())
    proto = _protocol()
    signed = sign_payload(cloudpickle.dumps({"func": _square, "args": (5,)}), master_key=key)
    frames = _chunks(signed)
    for frame in frames[:-1]:
        _run(proto, frame)
    status, body = _run(proto, frames[-1])
    assert status == STAT_OK
    assert cloudpickle.loads(body) == 25
