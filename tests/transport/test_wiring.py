"""Phase-2 wiring tests: cli + quic_server load mTLS material from ZAKURO_CERT_DIR (#115)."""

from __future__ import annotations

import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _write_self_signed(cert_dir: Path, *, common_name: str = "worker-1") -> None:
    cert_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    (cert_dir / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (cert_dir / "tls.key").write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (cert_dir / "ca.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture
def cert_dir(tmp_path: Path) -> Path:
    base = tmp_path / "certs"
    _write_self_signed(base)
    return base


# ---- HTTP transport (zakuro.worker.cli) -------------------------------------


def test_cli_passes_ssl_kwargs_when_cert_dir_set(
    monkeypatch: pytest.MonkeyPatch, cert_dir: Path
) -> None:
    """uvicorn.run receives ssl_certfile/ssl_keyfile/ssl_ca_certs only when ZAKURO_CERT_DIR is set."""
    monkeypatch.setenv("ZAKURO_CERT_DIR", str(cert_dir))
    monkeypatch.setattr("sys.argv", ["zakuro-worker", "--transport", "http", "--port", "0"])
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    with patch("uvicorn.run", side_effect=fake_run):
        from zakuro.worker.cli import main

        main()

    assert captured.get("ssl_certfile") == str(cert_dir / "tls.crt")
    assert captured.get("ssl_keyfile") == str(cert_dir / "tls.key")
    assert captured.get("ssl_ca_certs") == str(cert_dir / "ca.crt")
    # CERT_REQUIRED == 2 in the stdlib
    assert captured.get("ssl_cert_reqs") == ssl.CERT_REQUIRED


def test_cli_no_ssl_kwargs_when_cert_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZAKURO_CERT_DIR", raising=False)
    monkeypatch.setattr("sys.argv", ["zakuro-worker", "--transport", "http", "--port", "0", "--host", "127.0.0.1"])
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    with patch("uvicorn.run", side_effect=fake_run):
        from zakuro.worker.cli import main

        main()

    assert "ssl_certfile" not in captured
    assert "ssl_keyfile" not in captured
    assert "ssl_ca_certs" not in captured


def test_cli_fails_closed_when_cert_dir_missing_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pointing ZAKURO_CERT_DIR at an empty dir → load_server_tls raises → CLI bails before uvicorn.run."""
    monkeypatch.setenv("ZAKURO_CERT_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["zakuro-worker", "--transport", "http", "--port", "0"])

    from zakuro.transport import TlsNotConfiguredError
    from zakuro.worker.cli import main

    with patch("uvicorn.run") as run, pytest.raises(TlsNotConfiguredError):
        main()
    run.assert_not_called()


# ---- QUIC transport ---------------------------------------------------------


def test_quic_run_server_imports_resolve_for_mtls_branch(
    monkeypatch: pytest.MonkeyPatch, cert_dir: Path
) -> None:
    """The mTLS branch in ``_run_server`` resolves its imports at module-load time.

    The deferred imports in ``zakuro.worker.quic_server._run_server``
    are exercised here as a sanity check; a missing import would
    silently regress to the legacy self-signed path in production.
    Running the actual QUIC server end-to-end with mTLS is covered by
    the integration test, not by this unit.
    """
    pytest.importorskip("aioquic")
    monkeypatch.setenv("ZAKURO_CERT_DIR", str(cert_dir))

    from zakuro.transport import build_aioquic_server_configuration, load_server_tls

    material = load_server_tls()
    config = build_aioquic_server_configuration(material)
    assert "zk-worker-v2" in config.alpn_protocols  # type: ignore[attr-defined]
    assert config.is_client is False  # type: ignore[attr-defined]
