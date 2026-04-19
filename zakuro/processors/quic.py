"""QUIC client processor.

Talks to a Zakuro QUIC worker using the binary protocol defined in
``docs/PROTOCOL.md``. One connection per ``Compute`` instance, one
bidirectional stream per call. The public ``Processor`` API is sync; this
module owns a background event-loop thread that drives aioquic.
"""

from __future__ import annotations

import asyncio
import ssl
import threading
from typing import TYPE_CHECKING, Any, ClassVar, Optional

import cloudpickle

from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from zakuro.compute import Compute

OP_EXECUTE = 1
OP_INFO = 2
OP_HEALTH = 3

STAT_OK = 0
STAT_USER_ERROR = 1
STAT_PROTOCOL_ERROR = 2

ALPN = ["zk-worker"]
DEFAULT_PORT = 4433


# --------------------------------------------------------------------------- #
# Shared background event loop for all QuicProcessor instances.
# aioquic is asyncio-native; we bridge to the sync Processor API via
# ``run_coroutine_threadsafe``.
# --------------------------------------------------------------------------- #

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed() or not _loop.is_running():
            _loop = asyncio.new_event_loop()

            def _run() -> None:
                assert _loop is not None
                asyncio.set_event_loop(_loop)
                _loop.run_forever()

            _loop_thread = threading.Thread(
                target=_run, daemon=True, name="zakuro-quic-loop"
            )
            _loop_thread.start()
    return _loop


def _run_sync(coro: Any, timeout: Optional[float] = None) -> Any:
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


# --------------------------------------------------------------------------- #
# Client-side aioquic protocol: one future per outstanding stream.
# --------------------------------------------------------------------------- #


class WorkerQuicClientProtocol:
    """Lazy imports for aioquic; constructed only when the module is used."""

    __slots__ = ()


def _build_client_protocol_class() -> type:
    from aioquic.asyncio import QuicConnectionProtocol
    from aioquic.quic.events import StreamDataReceived

    class _Client(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._pending: dict[int, asyncio.Future] = {}
            self._bufs: dict[int, bytearray] = {}

        def quic_event_received(self, event: Any) -> None:  # noqa: D401
            if not isinstance(event, StreamDataReceived):
                return
            buf = self._bufs.setdefault(event.stream_id, bytearray())
            buf.extend(event.data)
            if len(buf) < 5:
                return
            expected = int.from_bytes(buf[1:5], "big")
            if len(buf) < 5 + expected:
                return
            status = buf[0]
            payload = bytes(buf[5 : 5 + expected])
            self._bufs.pop(event.stream_id, None)
            fut = self._pending.pop(event.stream_id, None)
            if fut is not None and not fut.done():
                fut.set_result((status, payload))

        async def request(self, op: int, payload: bytes) -> tuple[int, bytes]:
            stream_id = self._quic.get_next_available_stream_id(
                is_unidirectional=False
            )
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[stream_id] = fut
            frame = bytes([op]) + len(payload).to_bytes(4, "big") + payload
            self._quic.send_stream_data(stream_id, frame, end_stream=True)
            self.transmit()
            return await fut

    return _Client


_ClientProtocolClass: Optional[type] = None


def _client_protocol_class() -> type:
    global _ClientProtocolClass
    if _ClientProtocolClass is None:
        _ClientProtocolClass = _build_client_protocol_class()
    return _ClientProtocolClass


# --------------------------------------------------------------------------- #
# Processor
# --------------------------------------------------------------------------- #


class QuicProcessor(Processor):
    """Processor using QUIC transport to a Zakuro worker.

    URI scheme: ``quic://host:4433``.

    The processor is priority 20 (above HTTP's 10) so a worker advertising
    both should default to QUIC.
    """

    priority: ClassVar[int] = 20
    schemes: ClassVar[tuple[str, ...]] = ("quic",)

    def __init__(self, config: ProcessorConfig, compute: "Compute") -> None:
        super().__init__(config, compute)
        self._cm: Any = None
        self._protocol: Any = None

    @classmethod
    def is_available(cls) -> bool:
        try:
            import aioquic  # noqa: F401

            return True
        except ImportError:
            return False

    def connect(self) -> None:
        if self._connected:
            return
        _run_sync(self._async_connect())
        self._connected = True

    async def _async_connect(self) -> None:
        from aioquic.asyncio import connect
        from aioquic.quic.configuration import QuicConfiguration

        config = QuicConfiguration(
            is_client=True,
            alpn_protocols=ALPN,
            verify_mode=ssl.CERT_NONE,
        )
        self._cm = connect(
            self._config.host,
            self._config.port,
            configuration=config,
            create_protocol=_client_protocol_class(),
        )
        self._protocol = await self._cm.__aenter__()

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            _run_sync(self._async_disconnect(), timeout=3.0)
        finally:
            self._connected = False

    async def _async_disconnect(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
        self._protocol = None
        self._cm = None

    def execute(
        self,
        func_bytes: bytes,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if not self._connected or self._protocol is None:
            raise RuntimeError("QuicProcessor not connected; use as a context manager.")
        func = cloudpickle.loads(func_bytes)
        payload = cloudpickle.dumps(
            {"func": func, "args": args, "kwargs": kwargs}
        )
        status, body = _run_sync(self._protocol.request(OP_EXECUTE, payload))
        if status == STAT_OK:
            return cloudpickle.loads(body)
        if status == STAT_USER_ERROR:
            raise cloudpickle.loads(body)
        raise RuntimeError(
            f"QUIC protocol error from worker: {body.decode(errors='replace')}"
        )

    def info(self) -> dict[str, Any]:
        if not self._connected or self._protocol is None:
            raise RuntimeError("QuicProcessor not connected.")
        import json

        status, body = _run_sync(self._protocol.request(OP_INFO, b""))
        if status != STAT_OK:
            raise RuntimeError(f"info failed: {body!r}")
        return json.loads(body.decode())

    def ping(self) -> bool:
        if not self._connected or self._protocol is None:
            return False
        try:
            status, _ = _run_sync(self._protocol.request(OP_HEALTH, b""))
            return status == STAT_OK
        except Exception:
            return False
