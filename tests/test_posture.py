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


def test_validate_ok_with_defaults():
    P.validate_startup_config()  # no raise


def test_validate_rejects_wire_typo(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "true")  # not 'v1' nor a known off-token
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()


def test_validate_accepts_known_off_tokens(monkeypatch):
    for tok in ("", "legacy", "off"):
        monkeypatch.setenv("ZAKURO_WIRE", tok)
        P.validate_startup_config()  # no raise


def test_validate_rejects_auth_typo(monkeypatch):
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "tru")
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()


def test_validate_strict_without_key_fails(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")  # strict but no ZAKURO_HMAC_KEY*
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()


def test_validate_strict_with_key_ok(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", "00" * 32)  # valid hex
    P.validate_startup_config()  # no raise


def test_validate_rejects_bad_payload_cap(monkeypatch):
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "abc")
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()
