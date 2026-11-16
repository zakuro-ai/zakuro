"""Mutual TLS for the worker listeners (``ZAKURO_CERT_DIR``, ADR 0013 / #85).

All key material is generated at test time in ``tmp_path``; nothing is committed.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from zakuro.worker import tls
from zakuro.worker.posture import StartupConfigError


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _make_ca(cn: str):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _issue(ca, cn: str, directory: Path, stem: str) -> None:
    ca_key, ca_cert = ca
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / f"{stem}.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@pytest.fixture
def pki(tmp_path: Path):
    """Server dir (tls.crt/tls.key/ca.crt) plus good and rogue client cert dirs."""
    ca = _make_ca("zakuro-test-ca")
    rogue_ca = _make_ca("rogue-ca")
    server = tmp_path / "server"
    _issue(ca, "localhost", server, "tls")
    (server / "ca.crt").write_bytes(ca[1].public_bytes(serialization.Encoding.PEM))
    good = tmp_path / "good"
    _issue(ca, "client", good, "client")
    rogue = tmp_path / "rogue"
    _issue(rogue_ca, "client", rogue, "client")
    return {"server": server, "good": good, "rogue": rogue, "ca": server / "ca.crt"}


def _free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- unit


def test_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ZAKURO_CERT_DIR", raising=False)
    assert not tls.mtls_enabled()
    assert tls.uvicorn_ssl_kwargs() == {}
    monkeypatch.setenv("ZAKURO_CERT_DIR", "  ")
    assert not tls.mtls_enabled()


def test_uvicorn_kwargs_require_client_cert(monkeypatch, pki) -> None:
    monkeypatch.setenv("ZAKURO_CERT_DIR", str(pki["server"]))
    kw = tls.uvicorn_ssl_kwargs()
    assert kw["ssl_cert_reqs"] == ssl.CERT_REQUIRED == 2
    assert kw["ssl_certfile"].endswith("tls.crt")
    assert kw["ssl_keyfile"].endswith("tls.key")
    assert kw["ssl_ca_certs"].endswith("ca.crt")


@pytest.mark.parametrize("missing", ["tls.crt", "tls.key", "ca.crt"])
def test_incomplete_dir_fails_closed(monkeypatch, pki, missing) -> None:
    (pki["server"] / missing).unlink()
    monkeypatch.setenv("ZAKURO_CERT_DIR", str(pki["server"]))
    with pytest.raises(StartupConfigError, match=missing):
        tls.uvicorn_ssl_kwargs()


def test_cli_passes_ssl_kwargs_only_when_set(monkeypatch, pki) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    from zakuro.worker import cli

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    monkeypatch.setattr("sys.argv", ["zakuro-worker"])

    monkeypatch.delenv("ZAKURO_CERT_DIR", raising=False)
    monkeypatch.setenv("ZAKURO_INSECURE_BIND", "1")  # plaintext 0.0.0.0 bind: accept it
    cli.main()
    assert "ssl_cert_reqs" not in captured

    captured.clear()
    monkeypatch.setenv("ZAKURO_CERT_DIR", str(pki["server"]))
    cli.main()
    assert captured["ssl_cert_reqs"] == 2

    monkeypatch.setenv("ZAKURO_CERT_DIR", str(pki["server"] / "nope"))
    with pytest.raises(SystemExit):
        cli.main()


# ----------------------------------------------------------------- HTTP handshake


def test_http_mtls_handshake(monkeypatch, pki) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    httpx = pytest.importorskip("httpx")

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

        def ctx(client_dir: Path | None) -> ssl.SSLContext:
            c = ssl.create_default_context(cafile=str(pki["ca"]))
            if client_dir is not None:
                c.load_cert_chain(str(client_dir / "client.crt"), str(client_dir / "client.key"))
            return c

        # no client cert
        with httpx.Client(verify=ctx(None), timeout=5) as c, pytest.raises(httpx.HTTPError):
            c.get(url)

        # cert from the wrong CA
        with (
            httpx.Client(verify=ctx(pki["rogue"]), timeout=5) as c,
            pytest.raises(httpx.HTTPError),
        ):
            c.get(url)

        with httpx.Client(verify=ctx(pki["good"]), timeout=5) as c:
            r = c.get(url)
            assert r.status_code == 200 and r.text == "ok"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ----------------------------------------------------------------- QUIC handshake


def _udp_loopback_ok() -> bool:
    try:
        _free_port(socket.SOCK_DGRAM)
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _udp_loopback_ok(), reason="cannot bind loopback UDP")
def test_quic_mtls_handshake(pki) -> None:
    pytest.importorskip("aioquic")
    from aioquic.asyncio import QuicConnectionProtocol, connect, serve
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import HandshakeCompleted

    material = tls.load_server_tls(str(pki["server"]))
    completed: list[bool] = []  # one entry per server-side handshake that finished

    class Probe(QuicConnectionProtocol):
        def __init__(self, quic, *a, **kw) -> None:
            tls.require_quic_client_cert(quic, material)
            super().__init__(quic, *a, **kw)

        def quic_event_received(self, event) -> None:
            if isinstance(event, HandshakeCompleted):
                completed.append(True)

    async def run() -> tuple[bool, bool, bool]:
        port = _free_port(socket.SOCK_DGRAM)
        sconf = QuicConfiguration(alpn_protocols=["zk-worker"], is_client=False)
        tls.apply_quic_mtls(sconf, material)
        assert sconf.verify_mode == ssl.CERT_REQUIRED
        server = await serve("127.0.0.1", port, configuration=sconf, create_protocol=Probe)

        async def attempt(client_dir: Path | None) -> bool:
            """True iff the *server* completed a handshake for this client."""
            before = len(completed)
            conf = QuicConfiguration(alpn_protocols=["zk-worker"], is_client=True)
            conf.load_verify_locations(cafile=str(pki["ca"]))
            conf.server_name = "localhost"
            if client_dir is not None:
                conf.load_cert_chain(str(client_dir / "client.crt"), str(client_dir / "client.key"))

            async def handshake() -> None:
                async with connect("127.0.0.1", port, configuration=conf) as proto:
                    await proto.ping()
                    await asyncio.sleep(0.3)

            # asyncio.wait_for, not asyncio.timeout: CI also runs Python 3.10.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(handshake(), timeout=5)
            return len(completed) > before

        try:
            return (
                await attempt(None),
                await attempt(pki["rogue"]),
                await attempt(pki["good"]),
            )
        finally:
            server.close()

    none_ok, rogue_ok, good_ok = asyncio.run(run())
    assert not none_ok
    assert not rogue_ok
    assert good_ok


@pytest.mark.skipif(not _udp_loopback_ok(), reason="cannot bind loopback UDP")
def test_quic_worker_server_enforces_mtls(monkeypatch, pki) -> None:
    """The real ``_run_server`` wiring refuses a cert-less client and accepts a signed one."""
    pytest.importorskip("aioquic")
    from concurrent.futures import ThreadPoolExecutor

    from aioquic.asyncio import connect
    from aioquic.quic.configuration import QuicConfiguration

    from zakuro.worker import quic_server

    monkeypatch.setenv("ZAKURO_CERT_DIR", str(pki["server"]))

    async def run() -> tuple[bool, bool]:
        port = _free_port(socket.SOCK_DGRAM)
        ready = asyncio.Event()
        with ThreadPoolExecutor(max_workers=1) as ex:
            task = asyncio.create_task(quic_server._run_server("127.0.0.1", port, ex, ready))
            await asyncio.wait_for(ready.wait(), 10)

            async def attempt(client_dir: Path | None) -> bool:
                conf = QuicConfiguration(alpn_protocols=quic_server.ALPN, is_client=True)
                conf.load_verify_locations(cafile=str(pki["ca"]))
                conf.server_name = "localhost"
                if client_dir is not None:
                    conf.load_cert_chain(
                        str(client_dir / "client.crt"), str(client_dir / "client.key")
                    )

                async def handshake() -> bool:
                    async with connect("127.0.0.1", port, configuration=conf) as proto:
                        await proto.ping()
                        await asyncio.sleep(0.3)
                        return proto._quic._close_event is None

                try:
                    # asyncio.wait_for, not asyncio.timeout: CI also runs Python 3.10.
                    return await asyncio.wait_for(handshake(), timeout=5)
                except (Exception, asyncio.CancelledError):
                    return False

            try:
                return await attempt(None), await attempt(pki["good"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    none_ok, good_ok = asyncio.run(run())
    assert not none_ok
    assert good_ok
