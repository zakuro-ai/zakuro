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

from zakuro import __version__
from zakuro.auth.frame import STAT_UNAUTHORIZED, FrameAuthError, authorize_frame
from zakuro.worker.envelope import EnvelopeRejectedError, unwrap_payload
from zakuro.worker.posture import max_payload_bytes, validate_auth_config, validate_wire_config

logger = logging.getLogger(__name__)

OP_EXECUTE = 1
OP_INFO = 2
OP_HEALTH = 3

STAT_OK = 0
STAT_USER_ERROR = 1
STAT_PROTOCOL_ERROR = 2

ALPN = ["zk-worker"]
DEFAULT_PORT = 4433
_CERT_DIR = Path.home() / ".zakuro"
_CERT_PATH = _CERT_DIR / "quic_worker_cert.pem"
_KEY_PATH = _CERT_DIR / "quic_worker_key.pem"


def _execute_payload(payload: bytes) -> tuple[int, bytes]:
    """Run an EXECUTE payload and return ``(status, response_bytes)``.

    Honours the request ``action`` (``execute`` / ``create_instance`` /
    ``call_method``) so stateful ``@cls`` proxies work over QUIC exactly as
    over HTTP, sharing the worker's in-process instance store. Differentiates
    protocol errors (``stat=2``: malformed request) from user errors
    (``stat=1``: the call raised). Must run on a worker thread.
    """
    from zakuro.worker import executor as _executor

    try:
        payload = unwrap_payload(payload)  # ZAKURO_WIRE=v1: verify HMAC before any unpickling
    except EnvelopeRejectedError:
        return STAT_PROTOCOL_ERROR, b"unauthorized: invalid or unsigned envelope"
    try:
        data = cloudpickle.loads(payload)
    except Exception as exc:
        return STAT_PROTOCOL_ERROR, f"malformed cloudpickle: {exc!r}".encode()
    if not isinstance(data, dict):
        return STAT_PROTOCOL_ERROR, b"malformed request: expected a dict payload"

    action = data.get("action", "execute")
    try:
        if action == "execute" or "func" in data:
            func = data["func"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})

            def _run() -> Any:
                return func(*args, **kwargs)

        elif action == "create_instance":
            klass = data["klass"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})
            client_id = data.get("instance_id")

            def _run() -> Any:
                _executor._ensure_capacity(client_id)
                instance = klass(*args, **kwargs)
                if client_id:
                    return {"instance_id": _executor._put_instance(client_id, instance)}
                return {"instance_id": _executor._store_instance(instance)}

        elif action == "release_instance":
            release_id = data["instance_id"]

            def _run() -> Any:
                _executor._cleanup_instance(release_id)
                return {"released": True}

        elif action == "call_method":
            instance_id = data["instance_id"]
            method_name = data["method"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})

            def _run() -> Any:
                instance = _executor._get_instance(instance_id)
                return getattr(instance, method_name)(*args, **kwargs)

        else:
            return STAT_PROTOCOL_ERROR, f"unknown action: {action!r}".encode()
    except KeyError as exc:
        return STAT_PROTOCOL_ERROR, f"malformed request dict: missing key {exc}".encode()

    try:
        result = _run()
    except _executor.InstanceStoreFullError as exc:
        return STAT_PROTOCOL_ERROR, str(exc).encode()
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
            "version": __version__,
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


class PayloadTooLargeError(Exception):
    """A frame's declared length exceeds the inbound payload cap."""


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
            declared = int.from_bytes(self.buf[1:5], "big")
            limit = max_payload_bytes()
            if declared > limit:
                # Reject on the declared prefix, before buffering any payload.
                self.buf.clear()
                raise PayloadTooLargeError(f"declared payload {declared} exceeds cap {limit}")
            self.expected = declared
            del self.buf[:5]
        assert self.expected is not None
        if len(self.buf) < self.expected:
            return None
        payload = bytes(self.buf[: self.expected])
        del self.buf[: self.expected]
        return self.op, payload


class WorkerQuicProtocol(QuicConnectionProtocol):
    """One instance per accepted QUIC connection.

    Streams are independent; each carries exactly one request/response pair.
    """

    def __init__(self, *args: Any, executor: ThreadPoolExecutor, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._executor = executor
        self._streams: dict[int, _StreamBuffer] = {}

    def quic_event_received(self, event: Any) -> None:
        if isinstance(event, StreamDataReceived):
            sb = self._streams.setdefault(event.stream_id, _StreamBuffer())
            try:
                frame = sb.feed(event.data)
            except PayloadTooLargeError as exc:
                self._streams.pop(event.stream_id, None)
                self._quic.send_stream_data(
                    event.stream_id,
                    _frame(STAT_PROTOCOL_ERROR, str(exc).encode()),
                    end_stream=True,
                )
                self.transmit()
                return
            if frame is not None:
                self._streams.pop(event.stream_id, None)
                try:
                    op, payload = authorize_frame(frame[0], frame[1])
                except FrameAuthError:
                    # Refuse before anything is deserialized or executed.
                    self._quic.send_stream_data(
                        event.stream_id, _frame(STAT_UNAUTHORIZED, b""), end_stream=True
                    )
                    self.transmit()
                    return
                asyncio.ensure_future(self._handle_request(event.stream_id, op, payload))

    async def _handle_request(self, stream_id: int, op: int, payload: bytes) -> None:
        try:
            if op == OP_EXECUTE:
                loop = asyncio.get_event_loop()
                status, body = await loop.run_in_executor(self._executor, _execute_payload, payload)
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
    validate_auth_config()
    validate_wire_config()
    config = QuicConfiguration(
        alpn_protocols=ALPN,
        is_client=False,
        max_datagram_frame_size=65536,
    )
    from zakuro.worker.tls import (
        apply_quic_mtls,
        load_server_tls,
        mtls_enabled,
        require_quic_client_cert,
    )

    mtls_material = load_server_tls() if mtls_enabled() else None
    if mtls_material is not None:
        # ZAKURO_CERT_DIR: serve the configured cert and require client certs.
        apply_quic_mtls(config, mtls_material)
    else:
        cert_path, key_path = _ensure_certificate()
        config.load_cert_chain(str(cert_path), str(key_path))

    def _factory(*args: Any, **kwargs: Any) -> WorkerQuicProtocol:
        if mtls_material is not None:
            require_quic_client_cert(args[0], mtls_material)
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
