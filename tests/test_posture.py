# tests/test_posture.py
import pytest

from zakuro.worker import posture as P  # noqa: N812


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "ZAKURO_AUTH_REQUIRED", "ZAKURO_WIRE", "ZAKURO_CERT_DIR",
        "ZAKURO_INSECURE_BIND", "ZAKURO_HMAC_KEY", "ZAKURO_HMAC_KEY_FILE",
        "ZAKURO_MAX_PAYLOAD_BYTES",
    ):
        monkeypatch.delenv(k, raising=False)


def test_default_posture_is_unauthenticated():
    assert P.is_auth_required() is False
    assert P.is_wire_strict() is False
    assert P.is_mtls_enabled() is False
    assert P.is_caller_authenticated() is False


def test_auth_flag_truthy(monkeypatch):
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    assert P.is_auth_required() is True
    assert P.is_caller_authenticated() is True


def test_wire_strict_counts_as_authenticated(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    assert P.is_wire_strict() is True
    assert P.is_caller_authenticated() is True


def test_mtls_counts_as_authenticated(monkeypatch):
    monkeypatch.setenv("ZAKURO_CERT_DIR", "/etc/zakuro/certs")
    assert P.is_mtls_enabled() is True
    assert P.is_caller_authenticated() is True


def test_loopback_detection():
    assert P.is_loopback("127.0.0.1") is True
    assert P.is_loopback("::1") is True
    assert P.is_loopback("LOCALHOST") is True
    assert P.is_loopback("0.0.0.0") is False
    assert P.is_loopback("10.13.13.21") is False


def test_max_payload_default_is_256_mib():
    assert P.max_payload_bytes() == 256 * 1024 * 1024


def test_security_posture_includes_host_and_port():
    snap = P.security_posture(host="0.0.0.0", port=3960)
    assert snap["host"] == "0.0.0.0"
    assert snap["port"] == 3960
    assert snap["loopback"] is False
    assert snap["caller_authenticated"] is False
