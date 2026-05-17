"""Phase-3 wiring tests for the QUIC chunk handler (#175)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import cloudpickle
import pytest

from zakuro.wire import (
    WIRE_VERSION_V2,
    ChunkFrame,
    EnvelopeV2,
    ResourceLimits,
    encode_chunk_frame,
    encode_envelope_v2,
)

pytest.importorskip("aioquic")

from zakuro.worker.quic_server import (  # noqa: E402  -- aioquic must be importable first
    STAT_CHUNK_ACK,
    STAT_OK,
    STAT_PROTOCOL_ERROR,
    WorkerQuicProtocol,
)


def _build_envelope_v2(callable_blob: bytes, *, tenant: str = "tenant-acme") -> EnvelopeV2:
    return EnvelopeV2(
        version=WIRE_VERSION_V2,
        job_id="j-1",
        tenant_id=tenant,
        callable=callable_blob,
        args=b"",
        hmac=b"\x00" * 32,
        resource_limits=ResourceLimits(cpus=1.0, memory_mb=1024, gpus=0, timeout_seconds=30),
    )


def _instantiate_protocol() -> WorkerQuicProtocol:
    """Build a WorkerQuicProtocol skipping aioquic's base constructor.

    Going through aioquic's full QuicConnectionProtocol() requires a real
    QuicConnection + asyncio loop wiring we don't need for the chunk-handler
    unit tests. We bypass __init__ and set just the fields _handle_chunk reads.
    """
    proto = WorkerQuicProtocol.__new__(WorkerQuicProtocol)
    proto._executor = ThreadPoolExecutor(max_workers=1)
    from zakuro.wire.streaming import ChunkReassembler

    proto._reassembler = ChunkReassembler()
    return proto


# ---- happy path -------------------------------------------------------------


def test_chunked_dispatch_round_trips() -> None:
    """Multi-chunk envelope reassembles and the executor runs the callable."""

    def double(x: int) -> int:
        return x * 2

    callable_blob = cloudpickle.dumps({"action": "execute", "func": double, "args": (21,)})
    env_bytes = encode_envelope_v2(_build_envelope_v2(callable_blob))

    # Split into 3 chunks
    chunks = []
    chunk_size = max(1, len(env_bytes) // 3)
    for i, start in enumerate(range(0, len(env_bytes), chunk_size)):
        slice_ = env_bytes[start : start + chunk_size]
        is_last = (start + chunk_size) >= len(env_bytes)
        chunks.append(
            ChunkFrame(
                version=WIRE_VERSION_V2,
                stream_id=42,
                seq=i,
                last=is_last,
                bytes_=slice_,
            )
        )

    proto = _instantiate_protocol()

    async def run() -> list[tuple[int, bytes]]:
        return [await proto._handle_chunk(encode_chunk_frame(c)) for c in chunks]

    results = asyncio.run(run())

    # Intermediates → CHUNK_ACK + empty body
    for status, body in results[:-1]:
        assert status == STAT_CHUNK_ACK
        assert body == b""

    # Final → executor produced cloudpickle(42)
    status, body = results[-1]
    assert status == STAT_OK
    assert cloudpickle.loads(body) == 42


def test_single_chunk_dispatch() -> None:
    """A single-chunk dispatch (last=True on seq=0) executes immediately."""

    def square(x: int) -> int:
        return x * x

    callable_blob = cloudpickle.dumps({"action": "execute", "func": square, "args": (9,)})
    env_bytes = encode_envelope_v2(_build_envelope_v2(callable_blob))
    chunk = ChunkFrame(version=WIRE_VERSION_V2, stream_id=1, seq=0, last=True, bytes_=env_bytes)

    proto = _instantiate_protocol()
    status, body = asyncio.run(proto._handle_chunk(encode_chunk_frame(chunk)))
    assert status == STAT_OK
    assert cloudpickle.loads(body) == 81


# ---- error paths ------------------------------------------------------------


def test_malformed_chunk_payload_rejected() -> None:
    proto = _instantiate_protocol()
    status, body = asyncio.run(proto._handle_chunk(b"\xff\xff\xff garbage"))
    assert status == STAT_PROTOCOL_ERROR
    assert b"malformed chunk" in body


def test_out_of_order_chunks_rejected() -> None:
    """A chunk with seq=5 when seq=0 is expected is rejected protocol-error."""
    proto = _instantiate_protocol()
    chunk = ChunkFrame(version=WIRE_VERSION_V2, stream_id=1, seq=5, last=False, bytes_=b"x")
    status, body = asyncio.run(proto._handle_chunk(encode_chunk_frame(chunk)))
    assert status == STAT_PROTOCOL_ERROR
    assert b"chunk rejected" in body


def test_assembled_garbage_rejected_as_envelope() -> None:
    """A stream whose reassembled bytes are not a valid EnvelopeV2 → 401-ish protocol error."""
    proto = _instantiate_protocol()
    chunks = [
        ChunkFrame(version=WIRE_VERSION_V2, stream_id=7, seq=0, last=False, bytes_=b"hello"),
        ChunkFrame(version=WIRE_VERSION_V2, stream_id=7, seq=1, last=True, bytes_=b"world"),
    ]

    async def run() -> list[tuple[int, bytes]]:
        return [await proto._handle_chunk(encode_chunk_frame(c)) for c in chunks]

    results = asyncio.run(run())
    final_status, final_body = results[-1]
    assert final_status == STAT_PROTOCOL_ERROR
    assert b"reassembled envelope malformed" in final_body


def test_concurrent_streams_isolated() -> None:
    """Two interleaved stream_ids don't poison each other's reassembly."""

    def f_a(_: int) -> int:
        return 10

    def f_b(_: int) -> int:
        return 20

    env_a = encode_envelope_v2(
        _build_envelope_v2(cloudpickle.dumps({"action": "execute", "func": f_a, "args": (0,)}))
    )
    env_b = encode_envelope_v2(
        _build_envelope_v2(cloudpickle.dumps({"action": "execute", "func": f_b, "args": (0,)}))
    )

    proto = _instantiate_protocol()

    async def run() -> tuple[tuple[int, bytes], tuple[int, bytes]]:
        # Send first half of each, then second half (interleaved)
        await proto._handle_chunk(
            encode_chunk_frame(
                ChunkFrame(
                    version=WIRE_VERSION_V2,
                    stream_id=100,
                    seq=0,
                    last=False,
                    bytes_=env_a[: len(env_a) // 2],
                )
            )
        )
        await proto._handle_chunk(
            encode_chunk_frame(
                ChunkFrame(
                    version=WIRE_VERSION_V2,
                    stream_id=200,
                    seq=0,
                    last=False,
                    bytes_=env_b[: len(env_b) // 2],
                )
            )
        )
        # Final halves
        result_a = await proto._handle_chunk(
            encode_chunk_frame(
                ChunkFrame(
                    version=WIRE_VERSION_V2,
                    stream_id=100,
                    seq=1,
                    last=True,
                    bytes_=env_a[len(env_a) // 2 :],
                )
            )
        )
        result_b = await proto._handle_chunk(
            encode_chunk_frame(
                ChunkFrame(
                    version=WIRE_VERSION_V2,
                    stream_id=200,
                    seq=1,
                    last=True,
                    bytes_=env_b[len(env_b) // 2 :],
                )
            )
        )
        return result_a, result_b

    result_a, result_b = asyncio.run(run())
    assert result_a[0] == STAT_OK
    assert result_b[0] == STAT_OK
    assert cloudpickle.loads(result_a[1]) == 10
    assert cloudpickle.loads(result_b[1]) == 20
