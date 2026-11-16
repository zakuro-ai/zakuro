"""SDK client certificates for mTLS workers (``ZAKURO_CLIENT_CERT_DIR``, #100).

Reuses the throwaway-PKI helpers of ``test_worker_tls``; no key material is committed.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from tests.test_worker_tls import _free_port, _udp_loopback_ok
from tests.test_worker_tls import pki as pki  # re-exported pytest fixture
from zakuro import client_tls
from zakuro.client_tls import ClientTlsError


def _client_dir(src: Path, dest: Path, ca: Path) -> Path:
    """Lay a ``client.*`` cert dir out under the names the SDK expects."""
    dest.mkdir()
    shutil.copy(src / "client.crt", dest / "tls.crt")
    shutil.copy(src / "client.key", dest / "tls.key")
    shutil.copy(ca, dest / "ca.crt")
    return dest


def test_unset_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("ZAKURO_CLIENT_CERT_DIR", raising=False)
    assert client_tls.client_ssl_context() is None
    sentinel = object()
    assert client_tls.apply_quic_client_tls(sentinel) is sentinel
    monkeypatch.setenv("ZAKURO_CLIENT_CERT_DIR", "  ")
    assert client_tls.load_client_tls() is None


@pytest.mark.parametrize("missing", ["tls.crt", "tls.key", "ca.crt"])
def test_incomplete_dir_fails_closed(monkeypatch, pki, tmp_path, missing) -> None:
    d = _client_dir(pki["good"], tmp_path / "c", pki["ca"])
    (d / missing).unlink()
    monkeypatch.setenv("ZAKURO_CLIENT_CERT_DIR", str(d))
    with pytest.raises(ClientTlsError, match=missing):
        client_tls.client_ssl_context()
    from aioquic.quic.configuration import QuicConfiguration

    with pytest.raises(ClientTlsError, match=missing):
        client_tls.apply_quic_client_tls(QuicConfiguration(is_client=True))


def test_http_client_fails_closed_on_incomplete_dir(monkeypatch, tmp_path) -> None:
    from zakuro.client import ZakuroClient
    from zakuro.compute import Compute

    monkeypatch.setenv("ZAKURO_CLIENT_CERT_DIR", str(tmp_path / "nope"))
    with pytest.raises(ClientTlsError):
        _ = ZakuroClient(Compute(host="127.0.0.1", port=1)).client


def test_http_sdk_client_mtls_handshake(monkeypatch, pki, tmp_path) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    httpx = pytest.importorskip("httpx")
    from zakuro.client import ZakuroClient
    from zakuro.compute import Compute
    from zakuro.worker import tls

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    monkeypatch.setenv("ZAKURO_CERT_DIR", str(pki["server"]))
    port = _free_port(socket.SOCK_STREAM)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", **tls.uvicorn_ssl_kwargs()
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started
        url = f"https://127.0.0.1:{port}/"
        compute = Compute(host="127.0.0.1", port=port)

        # Unset: the SDK presents no cert, so the mTLS worker refuses it.
        monkeypatch.delenv("ZAKURO_CLIENT_CERT_DIR", raising=False)
        with (
            httpx.Client(timeout=5, verify=False) as plain,  # noqa: S501
            pytest.raises(httpx.HTTPError),
        ):
            plain.get(url)
        sdk = ZakuroClient(compute)
        with pytest.raises(httpx.HTTPError):
            sdk.client.get(url, timeout=5)

        # Cert from a rogue CA: refused.
        rogue = _client_dir(pki["rogue"], tmp_path / "sdk-rogue", pki["ca"])
        monkeypatch.setenv("ZAKURO_CLIENT_CERT_DIR", str(rogue))
        with pytest.raises(httpx.HTTPError):
            ZakuroClient(compute).client.get(url, timeout=5)

        # Good cert: connects, and the worker is verified against ca.crt.
        good = _client_dir(pki["good"], tmp_path / "sdk-good", pki["ca"])
        monkeypatch.setenv("ZAKURO_CLIENT_CERT_DIR", str(good))
        r = ZakuroClient(compute).client.get(url, timeout=5)
        assert r.status_code == 200 and r.text == "ok"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.skipif(not _udp_loopback_ok(), reason="cannot bind loopback UDP")
def test_quic_processor_mtls_handshake(monkeypatch, pki, tmp_path) -> None:
    pytest.importorskip("aioquic")
    from concurrent.futures import ThreadPoolExecutor

    from zakuro.compute import Compute
    from zakuro.processors.base import ProcessorConfig
    from zakuro.processors.quic import QuicProcessor
    from zakuro.worker import quic_server

    monkeypatch.setenv("ZAKURO_CERT_DIR", str(pki["server"]))
    rogue = _client_dir(pki["rogue"], tmp_path / "sdk-rogue", pki["ca"])
    good = _client_dir(pki["good"], tmp_path / "sdk-good", pki["ca"])

    async def run() -> dict[str, bool]:
        port = _free_port(socket.SOCK_DGRAM)
        ready = asyncio.Event()
        with ThreadPoolExecutor(max_workers=1) as ex:
            task = asyncio.create_task(quic_server._run_server("127.0.0.1", port, ex, ready))
            await asyncio.wait_for(ready.wait(), 10)

            async def attempt(client_dir: Path | None) -> bool:
                if client_dir is None:
                    monkeypatch.delenv("ZAKURO_CLIENT_CERT_DIR", raising=False)
                else:
                    monkeypatch.setenv("ZAKURO_CLIENT_CERT_DIR", str(client_dir))
                cfg = ProcessorConfig(scheme="quic", host="127.0.0.1", port=port)
                proc = QuicProcessor(cfg, Compute(host="127.0.0.1", port=port))

                async def handshake() -> bool:
                    await proc._async_connect()
                    try:
                        await proc._protocol.ping()
                        await asyncio.sleep(0.3)
                        return proc._protocol._quic._close_event is None
                    finally:
                        await proc._async_disconnect()

                try:
                    # asyncio.wait_for, not asyncio.timeout: CI also runs Python 3.10.
                    return await asyncio.wait_for(handshake(), timeout=5)
                except (Exception, asyncio.CancelledError):
                    return False

            try:
                return {
                    "none": await attempt(None),
                    "rogue": await attempt(rogue),
                    "good": await attempt(good),
                }
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    res = asyncio.run(run())
    assert res == {"none": False, "rogue": False, "good": True}
