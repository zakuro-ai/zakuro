"""QUIC worker server.

Binary, length-prefixed QUIC protocol replacing the HTTP ``/execute`` hot
path. See ``docs/PROTOCOL.md`` for the full wire spec. The semantics of the
EXECUTE payload are identical to the HTTP worker's ``/execute`` endpoint —
cloudpickle of ``{"func", "args", "kwargs"}`` in, cloudpickle of the result
(or exception) out — so callers can swap transport without touching payload
serialisation.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import multiprocessing
import os
import platform
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cloudpickle

try:
    from aioquic.asyncio import QuicConnectionProtocol, serve
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import StreamDataReceived
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "QUIC worker transport requires aioquic; install the `[worker]` extra."
    ) from exc

logger = logging.getLogger(__name__)

OP_EXECUTE = 1
OP_INFO = 2
OP_HEALTH = 3
# Streaming chunk for a multi-chunk dispatch (RFC 0001 amendment, #175).
# Each frame carries one ChunkFrame; the worker reassembles in order via
# zakuro.wire.streaming.ChunkReassembler and treats the concatenated bytes
# as a v0.2 EnvelopeV2 once last=True arrives.
OP_EXECUTE_CHUNK = 4

STAT_OK = 0
STAT_USER_ERROR = 1
STAT_PROTOCOL_ERROR = 2
# A chunk was accepted but more chunks are expected before the stream
# can be assembled. The caller continues sending chunks; only when the
# final chunk arrives does the worker emit OK + the execution body.
STAT_CHUNK_ACK = 3

ALPN = ["zk-worker"]
DEFAULT_PORT = 4433
_CERT_DIR = Path.home() / ".zakuro"
_CERT_PATH = _CERT_DIR / "quic_worker_cert.pem"
_KEY_PATH = _CERT_DIR / "quic_worker_key.pem"


def _execute_payload(payload: bytes) -> tuple[int, bytes]:
    """Run an EXECUTE payload and return ``(status, response_bytes)``.

    Wire format depends on ``ZAKURO_WIRE``:

    * ``v1`` — the payload is a postcard envelope. Decoded + HMAC-verified
      via :func:`zakuro.worker.envelope.unwrap_payload`; the inner blob
      is the cloudpickle dict to execute.
    * unset — legacy path: the payload is the cloudpickle dict directly.

    Differentiates protocol errors (``stat=2``: malformed envelope OR
    malformed cloudpickle) from user errors (``stat=1``: function raised).
    Must run on a worker thread.
    """
    # zakuro.worker.envelope.unwrap_payload is the single chokepoint that
    # decides whether to gate this request through postcard+HMAC or pass
    # the raw bytes through unchanged (legacy mode).
    from zakuro.wire import WireError as _WireError
    from zakuro.worker.envelope import unwrap_payload

    try:
        inner_payload, _envelope = unwrap_payload(payload)
    except _WireError as exc:
        return STAT_PROTOCOL_ERROR, f"envelope rejected: {exc!r}".encode()

    try:
        data = cloudpickle.loads(inner_payload)
    except Exception as exc:
        return STAT_PROTOCOL_ERROR, f"malformed cloudpickle: {exc!r}".encode()
    try:
        func = data["func"]
        args = data.get("args", ())
        kwargs = data.get("kwargs", {})
    except Exception as exc:
        return STAT_PROTOCOL_ERROR, f"malformed request dict: {exc!r}".encode()
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        return STAT_USER_ERROR, cloudpickle.dumps(exc)
    return STAT_OK, cloudpickle.dumps(result)


def _info_payload() -> bytes:
    """Build the INFO JSON response body. Shape mirrors HTTP ``/info``."""
    cpus = multiprocessing.cpu_count()
    try:
        import psutil

        vm = psutil.virtual_memory()
        memory_total, memory_available = vm.total, vm.available
    except ImportError:
        memory_total = 8 * 1024**3
        memory_available = memory_total
    return json.dumps(
        {
            "name": os.environ.get("ZAKURO_WORKER_NAME", f"worker-{platform.node()}"),
            "worker_type": os.environ.get("ZAKURO_WORKER_TYPE", "zakuro"),
            "version": "0.2.0",
            "transport": "quic",
            "resources": {
                "cpus_total": float(cpus),
                "cpus_available": float(cpus),
                "memory_total": memory_total,
                "memory_available": memory_available,
                "gpus_total": 0,
                "gpus_available": 0,
            },
        },
        separators=(",", ":"),
    ).encode()


def _frame(status: int, payload: bytes) -> bytes:
    return bytes([status]) + len(payload).to_bytes(4, "big") + payload


class _StreamBuffer:
    """Per-stream framing state: accumulates bytes until a full request is in."""

    __slots__ = ("buf", "op", "expected")

    def __init__(self) -> None:
        self.buf = bytearray()
        self.op: int | None = None
        self.expected: int | None = None

    def feed(self, data: bytes) -> tuple[int, bytes] | None:
        """Append bytes; return ``(op, payload)`` once a full frame is ready."""
        self.buf.extend(data)
        if self.op is None:
            if len(self.buf) < 5:
                return None
            self.op = self.buf[0]
            self.expected = int.from_bytes(self.buf[1:5], "big")
            del self.buf[:5]
        assert self.expected is not None
        if len(self.buf) < self.expected:
            return None
        payload = bytes(self.buf[: self.expected])
        del self.buf[: self.expected]
        return self.op, payload


class WorkerQuicProtocol(QuicConnectionProtocol):
    """One instance per accepted QUIC connection.

    QUIC streams are independent; the *framing* layer (this protocol)
    treats each stream as carrying exactly one request/response pair.

    Multi-chunk dispatches (#175 Phase 3) span multiple QUIC streams
    that share a single logical ``ChunkFrame.stream_id``. The per-
    connection :class:`ChunkReassembler` accumulates them; the *final*
    chunk's QUIC stream is the one that carries the execution result.
    """

    def __init__(self, *args: Any, executor: ThreadPoolExecutor, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._executor = executor
        self._streams: dict[int, _StreamBuffer] = {}
        # Lazy import: zakuro.wire.streaming pulls in serde_bytes types that
        # are not needed for v0.1 traffic. Importing here keeps the
        # module-load cost honest for v0.1-only deployments.
        from zakuro.wire.streaming import ChunkReassembler

        self._reassembler = ChunkReassembler()

    def quic_event_received(self, event: Any) -> None:
        if isinstance(event, StreamDataReceived):
            sb = self._streams.setdefault(event.stream_id, _StreamBuffer())
            frame = sb.feed(event.data)
            if frame is not None:
                self._streams.pop(event.stream_id, None)
                asyncio.ensure_future(self._handle_request(event.stream_id, frame[0], frame[1]))

    async def _handle_request(self, stream_id: int, op: int, payload: bytes) -> None:
        try:
            if op == OP_EXECUTE:
                loop = asyncio.get_event_loop()
                status, body = await loop.run_in_executor(self._executor, _execute_payload, payload)
            elif op == OP_EXECUTE_CHUNK:
                status, body = await self._handle_chunk(payload)
            elif op == OP_INFO:
                status, body = STAT_OK, _info_payload()
            elif op == OP_HEALTH:
                status, body = STAT_OK, b'{"status":"ok"}'
            else:
                status, body = (
                    STAT_PROTOCOL_ERROR,
                    f"unknown opcode: {op}".encode(),
                )
        except Exception as exc:  # defensive — worker bug
            logger.exception("unhandled server error")
            status, body = STAT_PROTOCOL_ERROR, f"server: {exc!r}".encode()

        self._quic.send_stream_data(stream_id, _frame(status, body), end_stream=True)
        self.transmit()

    async def _handle_chunk(self, payload: bytes) -> tuple[int, bytes]:
        """Decode a ChunkFrame, feed the reassembler, dispatch on last=True.

        Returns:
            ``(STAT_CHUNK_ACK, b"")`` while more chunks are expected.
            ``(STAT_OK, body)`` on the final chunk after execution finishes.
            ``(STAT_PROTOCOL_ERROR, reason)`` on malformed / out-of-order
            chunks or on rejected reassembled envelopes.
        """
        # Deferred imports for the same reason as the reassembler in __init__.
        from zakuro.wire import (
            WireError,
            decode_chunk_frame,
            decode_envelope_v2,
        )
        from zakuro.wire.streaming import StreamingError

        try:
            chunk = decode_chunk_frame(payload)
        except WireError as exc:
            return STAT_PROTOCOL_ERROR, f"malformed chunk: {exc!r}".encode()

        try:
            assembled = self._reassembler.feed(chunk)
        except StreamingError as exc:
            return STAT_PROTOCOL_ERROR, f"chunk rejected: {exc!r}".encode()

        if assembled is None:
            # Intermediate chunk — ack only, no execution yet.
            return STAT_CHUNK_ACK, b""

        # Final chunk: assembled bytes are a postcard EnvelopeV2. Decode +
        # forward the callable to the executor exactly like v0.1.
        try:
            envelope_v2 = decode_envelope_v2(assembled)
        except WireError as exc:
            return STAT_PROTOCOL_ERROR, f"reassembled envelope malformed: {exc!r}".encode()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, _execute_payload, envelope_v2.callable)


def _ensure_certificate() -> tuple[Path, Path]:
    """Generate a self-signed cert + key if absent; return their paths."""
    if _CERT_PATH.exists() and _KEY_PATH.exists():
        return _CERT_PATH, _KEY_PATH

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    _CERT_DIR.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * 5))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    _CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return _CERT_PATH, _KEY_PATH


async def _run_server(
    host: str,
    port: int,
    executor: ThreadPoolExecutor,
    ready: asyncio.Event | None,
) -> None:
    # mTLS rollout (#115 Phase 2): when ZAKURO_CERT_DIR is set, build the
    # QuicConfiguration via the shared zakuro.transport.tls helper so
    # client certs are required + the ALPN bumps to zk-worker-v2 (matching
    # the wire-format bump in RFC 0001). When unset, fall through to the
    # self-signed dev cert that previous releases used.
    if os.environ.get("ZAKURO_CERT_DIR", "").strip():
        from zakuro.transport import build_aioquic_server_configuration, load_server_tls

        config = build_aioquic_server_configuration(load_server_tls())
        config.max_datagram_frame_size = 65536
    else:
        cert_path, key_path = _ensure_certificate()
        config = QuicConfiguration(
            alpn_protocols=ALPN,
            is_client=False,
            max_datagram_frame_size=65536,
        )
        config.load_cert_chain(str(cert_path), str(key_path))

    def _factory(*args: Any, **kwargs: Any) -> WorkerQuicProtocol:
        return WorkerQuicProtocol(*args, executor=executor, **kwargs)

    server = await serve(
        host=host,
        port=port,
        configuration=config,
        create_protocol=_factory,
    )
    logger.info("QUIC worker listening on %s:%s", host, port)
    if ready is not None:
        ready.set()

    stop_event = asyncio.Event()
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Windows or sub-thread: callers drive the stop via ready/cancel.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    finally:
        server.close()


def run_quic_worker(
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    max_workers: int | None = None,
) -> None:
    """Blocking entry point. Runs the QUIC worker until SIGINT/SIGTERM."""
    executor = ThreadPoolExecutor(
        max_workers=max_workers or multiprocessing.cpu_count(),
    )
    try:
        asyncio.run(_run_server(host, port, executor, ready=None))
    finally:
        executor.shutdown(wait=True)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Zakuro QUIC worker")
    parser.add_argument("--host", default=os.environ.get("ZAKURO_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ZAKURO_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument("--worker-name", default=None)
    args = parser.parse_args()
    if args.worker_name:
        os.environ["ZAKURO_WORKER_NAME"] = args.worker_name
    logging.basicConfig(level=logging.INFO)
    run_quic_worker(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
