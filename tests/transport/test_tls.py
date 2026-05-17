"""Unit tests for the mTLS transport substrate (#115, RFC 0002)."""

from __future__ import annotations

import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from zakuro.transport import (
    TlsNotConfiguredError,
    build_aioquic_server_configuration,
    build_httpx_client,
    load_client_tls,
    load_server_tls,
)
from zakuro.transport.tls import build_client_ssl_context, build_server_ssl_context


def _write_cert(cert_dir: Path, *, common_name: str) -> None:
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
    target = tmp_path / "certs"
    _write_cert(target, common_name="zakuro-worker-test")
    return target


def test_load_server_tls_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_CERT_DIR", raising=False)
    with pytest.raises(TlsNotConfiguredError):
        load_server_tls()


def test_load_server_tls_raises_when_file_missing(tmp_path: Path) -> None:
    with pytest.raises(TlsNotConfiguredError):
        load_server_tls(cert_dir=str(tmp_path))


def test_load_server_tls_happy_path(cert_dir: Path) -> None:
    material = load_server_tls(cert_dir=str(cert_dir))
    assert material.cert_path == cert_dir / "tls.crt"
    assert material.key_path == cert_dir / "tls.key"
    assert material.ca_bundle_path == cert_dir / "ca.crt"


def test_load_server_tls_respects_env(monkeypatch: pytest.MonkeyPatch, cert_dir: Path) -> None:
    monkeypatch.setenv("ZAKURO_CERT_DIR", str(cert_dir))
    material = load_server_tls()
    assert material.cert_path == cert_dir / "tls.crt"


def test_load_client_tls_is_symmetric(cert_dir: Path) -> None:
    a = load_server_tls(cert_dir=str(cert_dir))
    b = load_client_tls(cert_dir=str(cert_dir))
    assert a == b


def test_build_server_ssl_context_requires_peer_cert(cert_dir: Path) -> None:
    material = load_server_tls(cert_dir=str(cert_dir))
    ctx = build_server_ssl_context(material)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.check_hostname is False


def test_build_client_ssl_context_requires_peer_cert(cert_dir: Path) -> None:
    material = load_client_tls(cert_dir=str(cert_dir))
    ctx = build_client_ssl_context(material)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3


def test_build_aioquic_server_configuration(cert_dir: Path) -> None:
    pytest.importorskip("aioquic")
    material = load_server_tls(cert_dir=str(cert_dir))
    config = build_aioquic_server_configuration(material)
    assert config.is_client is False
    assert "zk-worker-v2" in config.alpn_protocols
    assert config.verify_mode == ssl.CERT_REQUIRED


def test_build_httpx_client(cert_dir: Path) -> None:
    material = load_client_tls(cert_dir=str(cert_dir))
    client = build_httpx_client(material, base_url="https://example.test")
    # httpx accepts an SSLContext via `verify`
    assert client is not None
    client.close()
