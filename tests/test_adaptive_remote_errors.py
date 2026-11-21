"""The caller's own exception is not a dispatch failure (Wave 0, task 4).

``AdaptiveCompute`` retried on any exception, so a remote function that raised
``ValueError`` ran again on another worker, and its worker was struck for it.
Exceptions that come back from the worker are now marked
(``zakuro.errors.mark_remote``): they are raised once, untouched, and the
worker counts as healthy. A failure to reach the worker is still retried.
"""

from __future__ import annotations

import http.server
import threading
from typing import Any

import cloudpickle
import httpx
import pytest

from zakuro.adaptive import AdaptiveCompute
from zakuro.compute import Compute
from zakuro.errors import is_remote, mark_remote


def _worker(port: int) -> Compute:
    return Compute(uri=f"zc://127.0.0.1:{port}", verify=False)


class _RaisingFn:
    """Fn stand-in whose execution raises ``make_exc()`` every time."""

    def __init__(self, make_exc) -> None:  # noqa: ANN001
        self.calls = 0
        self.make_exc = make_exc
        self._compute = None

        def _f() -> None:
            return None

        self._func = _f

    def to(self, compute):  # noqa: ANN001
        self._compute = compute
        return self

    def _execute_single_compute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        exc = self.make_exc(self.calls)
        if exc is not None:
            raise exc
        return "ok"


def _adaptive(**kw: Any) -> AdaptiveCompute:
    return AdaptiveCompute(
        workers=[_worker(9001), _worker(9002)],
        softmax_temperature=0.0,
        initial_latency=0.001,
        **kw,
    )


def test_a_remote_exception_is_raised_once_without_retry():
    ac = _adaptive(max_dispatch_retries=2, eject_after_failures=1)
    fn = _RaisingFn(lambda n: mark_remote(ValueError("bad input")))
    with pytest.raises(ValueError, match="bad input"):
        ac.dispatch(fn, (), {})
    assert fn.calls == 1
    assert all(not s["suspended"] for s in ac.stats())
    assert sum(s["failures"] for s in ac.stats()) == 0


def test_a_remote_connection_error_is_not_retried_either():
    ac = _adaptive(max_dispatch_retries=2)
    fn = _RaisingFn(lambda n: mark_remote(ConnectionError("raised by the user's code")))
    with pytest.raises(ConnectionError):
        ac.dispatch(fn, (), {})
    assert fn.calls == 1


def test_a_transport_failure_is_still_retried():
    ac = _adaptive(max_dispatch_retries=1)
    fn = _RaisingFn(lambda n: httpx.ConnectError("refused") if n == 1 else None)
    assert ac.dispatch(fn, (), {}) == "ok"
    assert fn.calls == 2


def test_mark_remote_returns_the_same_exception():
    exc = ValueError("x")
    assert mark_remote(exc) is exc
    assert is_remote(exc)
    assert not is_remote(ValueError("y"))


class _PickledReplyBroker:
    """Answers every POST with ``reply`` (default: a pickled ``ValueError``)."""

    def __init__(self, reply: bytes | None = None) -> None:
        reply = cloudpickle.dumps(ValueError("from the worker")) if reply is None else reply

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = reply
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_the_broker_processor_marks_a_returned_exception(monkeypatch):
    import zakuro.mesh
    from zakuro.processors.base import ProcessorConfig
    from zakuro.processors.broker import BrokerProcessor

    monkeypatch.setattr(zakuro.mesh, "api_key", lambda: None)
    broker = _PickledReplyBroker()
    try:
        proc = BrokerProcessor(
            ProcessorConfig.from_uri(f"zc://127.0.0.1:{broker.port}"), _worker(broker.port)
        )
        with proc, pytest.raises(ValueError, match="from the worker") as info:
            proc.execute(cloudpickle.dumps(len), ([1],), {})
        assert is_remote(info.value)
    finally:
        broker.stop()


def test_the_http_processor_marks_a_returned_exception():
    from zakuro.processors.http import HttpProcessor

    proc = HttpProcessor.__new__(HttpProcessor)
    proc._connected = True

    class _Client:
        def execute(self, payload: bytes) -> bytes:
            return cloudpickle.dumps(ValueError("from the worker"))

    proc._client = _Client()
    with pytest.raises(ValueError) as info:
        proc.execute(cloudpickle.dumps(len), ([1],), {})
    assert is_remote(info.value)


def test_a_remote_instance_call_error_is_marked(monkeypatch):
    from zakuro.proxy import RemoteProxy

    proxy = RemoteProxy.__new__(RemoteProxy)
    proxy._instance_id = "instance_x"
    monkeypatch.setattr(
        proxy, "_send", lambda payload, action, iid: cloudpickle.dumps(KeyError("from the worker"))
    )
    with pytest.raises(KeyError) as info:
        proxy._call_remote_method("get", (), {})
    assert is_remote(info.value)


class _UnbuildableError(Exception):
    """An exception whose constructor takes two arguments. Pickling keeps only
    ``args`` (the message), so the caller cannot rebuild it."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def test_a_reply_that_cannot_be_rebuilt_is_still_the_callers_own(monkeypatch):
    # The call ran on the worker; only rebuilding its exception here failed.
    # Retrying would run the user's code a second time.
    import zakuro.mesh
    from zakuro.processors.base import ProcessorConfig
    from zakuro.processors.broker import BrokerProcessor

    monkeypatch.setattr(zakuro.mesh, "api_key", lambda: None)
    broker = _PickledReplyBroker(cloudpickle.dumps(_UnbuildableError(7, "from the worker")))
    try:
        proc = BrokerProcessor(
            ProcessorConfig.from_uri(f"zc://127.0.0.1:{broker.port}"), _worker(broker.port)
        )
        with proc, pytest.raises(Exception) as info:
            proc.execute(cloudpickle.dumps(len), ([1],), {})
        assert is_remote(info.value)
        assert "could not be rebuilt" in str(info.value)
    finally:
        broker.stop()


def test_the_quic_processor_marks_a_returned_exception(monkeypatch):
    from zakuro.processors.quic import STAT_USER_ERROR, QuicProcessor

    proc = QuicProcessor.__new__(QuicProcessor)
    monkeypatch.setattr(
        proc,
        "_request_execute",
        lambda payload: (STAT_USER_ERROR, cloudpickle.dumps(ValueError("from the worker"))),
    )
    with pytest.raises(ValueError) as info:
        proc.execute(cloudpickle.dumps(len), ([1],), {})
    assert is_remote(info.value)


def test_a_worker_that_could_not_load_the_call_is_not_the_callers_error():
    from zakuro.errors import mark_load_failure

    sent = mark_load_failure(ModuleNotFoundError("No module named 'pandas'"))
    received = cloudpickle.loads(cloudpickle.dumps(sent))
    assert not is_remote(mark_remote(received))  # another worker may have the module


def test_a_load_failure_is_retried_on_another_worker():
    from zakuro.errors import mark_load_failure

    ac = _adaptive(max_dispatch_retries=1)
    fn = _RaisingFn(
        lambda n: (
            mark_remote(mark_load_failure(ModuleNotFoundError("no pandas"))) if n == 1 else None
        )
    )
    assert ac.dispatch(fn, (), {}) == "ok"
    assert fn.calls == 2
