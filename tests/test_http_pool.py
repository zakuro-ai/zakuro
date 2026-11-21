"""One HTTP client per thread per broker (Wave 0, task 1).

Every SDK call used to build a fresh ``httpx.Client`` and close it again, so
each call paid for a new TCP connection to the broker. These tests pin the
reuse, and the two cases where reuse must not happen: another thread, and a
forked child process.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import socket
import threading
from typing import Any

import cloudpickle
import httpx
import pytest

from zakuro.processors import _http_pool


@pytest.fixture(autouse=True)
def _fresh_pool():
    _http_pool.close_all()
    yield
    _http_pool.close_all()


def _client(headers: dict[str, str] | None = None, url: str = "http://127.0.0.1:9") -> httpx.Client:
    return _http_pool.shared_client(
        url, headers or {"Authorization": "Bearer k"}, None, httpx.Timeout(5.0)
    )


def test_same_thread_same_key_reuses_the_client():
    assert _client() is _client()


def test_other_headers_get_another_client():
    assert _client({"Authorization": "Bearer a"}) is not _client({"Authorization": "Bearer b"})


def test_other_thread_gets_another_client():
    mine = _client()
    theirs: list[httpx.Client] = []
    t = threading.Thread(target=lambda: theirs.append(_client()))
    t.start()
    t.join()
    assert theirs and theirs[0] is not mine


def test_a_forked_child_does_not_reuse_the_parent_client(monkeypatch):
    parent = _client()
    monkeypatch.setattr(os, "getpid", lambda: os.getppid() + 999_999)
    assert _client() is not parent


def test_close_all_closes_and_forgets():
    first = _client()
    _http_pool.close_all()
    assert first.is_closed
    assert _client() is not first


class _Stub:
    """An HTTP/1.1 broker stand-in that answers every /execute with 5 and
    counts the TCP connections it accepted."""

    def __init__(self, port: int = 0) -> None:
        self.accepted = 0
        self.sockets: list[socket.socket] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                stub.accepted += 1
                stub.sockets.append(self.connection)

            def do_POST(self) -> None:  # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = cloudpickle.dumps(5)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop like a restarting broker: the listener and every kept
        connection go away."""
        self.server.shutdown()
        self.server.server_close()
        for s in self.sockets:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            s.close()


@pytest.fixture
def no_key(monkeypatch):
    import zakuro.mesh

    monkeypatch.setattr(zakuro.mesh, "api_key", lambda: None)


def _add(a: int, b: int) -> int:
    return a + b


def _remote_add(port: int):
    import zakuro
    from zakuro.compute import Compute

    return zakuro.fn(_add).to(Compute(uri=f"zc://127.0.0.1:{port}", verify=False))


def test_two_calls_use_one_tcp_connection(no_key):
    stub = _Stub()
    try:
        add = _remote_add(stub.port)
        assert add(2, 3) == 5
        assert add(2, 3) == 5
        assert stub.accepted == 1
    finally:
        stub.stop()


def test_a_broker_restart_between_calls_is_survived(no_key):
    stub = _Stub()
    port = stub.port
    add = _remote_add(port)
    assert add(2, 3) == 5
    stub.stop()
    stub = _Stub(port)
    try:
        assert add(2, 3) == 5
    finally:
        stub.stop()
