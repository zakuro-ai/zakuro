"""QUIC client processor.

Talks to a Zakuro QUIC worker using the binary protocol defined in
``docs/PROTOCOL.md``. One connection per ``Compute`` instance, one
bidirectional stream per call. The public ``Processor`` API is sync; this
module owns a background event-loop thread that drives aioquic.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import threading
from typing import TYPE_CHECKING, Any, ClassVar, cast

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

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
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

            _loop_thread = threading.Thread(target=_run, daemon=True, name="zakuro-quic-loop")
            _loop_thread.start()
    return _loop


def _run_sync(coro: Any, timeout: float | None = None) -> Any:
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
    from aioquic.quic.events import (
        ConnectionTerminated,
        StreamDataReceived,
        StreamReset,
    )

    class _Client(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._pending: dict[int, asyncio.Future] = {}
            self._bufs: dict[int, bytearray] = {}

        def quic_event_received(self, event: Any) -> None:  # noqa: D401
            if isinstance(event, StreamDataReceived):
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
            elif isinstance(event, (ConnectionTerminated, StreamReset)):
                # The underlying connection / stream went away before the
                # server finished responding. Fail every pending request
                # with ConnectionError so the caller can decide whether to
                # reconnect and retry.
                err = ConnectionError(
                    f"QUIC connection terminated: {getattr(event, 'reason_phrase', '')}"
                    or "QUIC connection dropped"
                )
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(err)
                self._pending.clear()

        async def request(self, op: int, payload: bytes) -> tuple[int, bytes]:
            stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
            fut: asyncio.Future[tuple[int, bytes]] = asyncio.get_event_loop().create_future()
            self._pending[stream_id] = fut
            frame = bytes([op]) + len(payload).to_bytes(4, "big") + payload
            self._quic.send_stream_data(stream_id, frame, end_stream=True)
            self.transmit()
            return await fut

    return _Client


_ClientProtocolClass: type | None = None


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

    def __init__(self, config: ProcessorConfig, compute: Compute) -> None:
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
            # Default aioquic idle timeout is very long (30–60 s). For a
            # distributed-compute pool, 5 s is plenty — anything longer is
            # an unrecoverable failure and the caller would rather see
            # ConnectionError quickly so the allocator can route around it.
            idle_timeout=5.0,
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
        payload = cloudpickle.dumps({"func": func, "args": args, "kwargs": kwargs})

        # One retry on connection-level failure: if the underlying QUIC
        # connection dropped (worker bounced, network blip, stale pool
        # connection), tear it down, dial again, and re-dispatch once.
        # Protocol-level errors from the worker (stat != OK) are NOT
        # retried — that's a deterministic failure the caller should see.
        try:
            status, body = _run_sync(self._protocol.request(OP_EXECUTE, payload))
        except (ConnectionError, OSError, RuntimeError) as first_exc:
            if not self._reconnect():
                # Propagate the original failure — caller can decide what
                # to do. Reconnect attempt itself logs via Worker / Adaptive.
                raise first_exc
            status, body = _run_sync(self._protocol.request(OP_EXECUTE, payload))

        if status == STAT_OK:
            return cloudpickle.loads(body)
        if status == STAT_USER_ERROR:
            raise cloudpickle.loads(body)
        raise RuntimeError(f"QUIC protocol error from worker: {body.decode(errors='replace')}")

    def _reconnect(self) -> bool:
        """Tear down the current connection and dial again. Returns ``True``
        on success."""
        with contextlib.suppress(Exception):
            _run_sync(self._async_disconnect(), timeout=3.0)
        self._connected = False
        try:
            _run_sync(self._async_connect())
            self._connected = True
            return True
        except Exception:
            return False

    def info(self) -> dict[str, Any]:
        if not self._connected or self._protocol is None:
            raise RuntimeError("QuicProcessor not connected.")
        import json

        status, body = _run_sync(self._protocol.request(OP_INFO, b""))
        if status != STAT_OK:
            raise RuntimeError(f"info failed: {body!r}")
        return cast("dict[str, Any]", json.loads(body.decode()))

    def ping(self) -> bool:
        if not self._connected or self._protocol is None:
            return False
        try:
            status, _ = _run_sync(self._protocol.request(OP_HEALTH, b""))
            return bool(status == STAT_OK)
        except Exception:
            return False
