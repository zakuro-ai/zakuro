import pytest

from zakuro.worker.envelope import EnvelopeRejectedError, unwrap_payload


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("ZAKURO_WIRE", "ZAKURO_MAX_PAYLOAD_BYTES"):
        monkeypatch.delenv(k, raising=False)


def test_oversize_legacy_body_rejected(monkeypatch):
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")
    with pytest.raises(EnvelopeRejectedError):
        unwrap_payload(b"x" * 17)


def test_undersize_legacy_body_passes(monkeypatch):
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")
    inner, env = unwrap_payload(b"x" * 10)
    assert inner == b"x" * 10
    assert env is None


def test_oversize_strict_body_rejected_before_decode(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", "00" * 32)
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")
    with pytest.raises(EnvelopeRejectedError):
        unwrap_payload(b"x" * 64)  # rejected on size, never reaches HMAC
