"""Payload cap: posture.max_payload_bytes and the QUIC framing guard (#36)."""

from __future__ import annotations

import pytest

pytest.importorskip("aioquic")

from zakuro.worker.posture import StartupConfigError, max_payload_bytes  # noqa: E402
from zakuro.worker.quic_server import OP_EXECUTE, PayloadTooLargeError, _StreamBuffer  # noqa: E402


def test_default_cap_is_256_mib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_MAX_PAYLOAD_BYTES", raising=False)
    assert max_payload_bytes() == 256 * 1024 * 1024


def test_cap_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "1024")
    assert max_payload_bytes() == 1024


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_invalid_cap_raises(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", bad)
    with pytest.raises(StartupConfigError):
        max_payload_bytes()


def test_stream_buffer_rejects_oversize_prefix_before_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "100")
    sb = _StreamBuffer()
    header = bytes([OP_EXECUTE]) + (101).to_bytes(4, "big")
    with pytest.raises(PayloadTooLargeError):
        sb.feed(header)
    assert len(sb.buf) == 0


def test_stream_buffer_accepts_frame_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "4")
    sb = _StreamBuffer()
    frame = sb.feed(bytes([OP_EXECUTE]) + (4).to_bytes(4, "big") + b"abcd")
    assert frame == (OP_EXECUTE, b"abcd")


def test_is_auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from zakuro.worker.posture import is_auth_required

    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    assert is_auth_required() is False
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", v)
        assert is_auth_required() is True
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "0")
    assert is_auth_required() is False


def test_validate_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from zakuro.worker.posture import validate_auth_config

    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    validate_auth_config()  # off -> no-op
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    monkeypatch.delenv("ZAKURO_JWT_PUBLIC_KEY_PEM", raising=False)
    with pytest.raises(StartupConfigError, match="ZAKURO_JWT_PUBLIC_KEY_PEM"):
        validate_auth_config()
    monkeypatch.setenv("ZAKURO_JWT_PUBLIC_KEY_PEM", "-----BEGIN PUBLIC KEY-----")
    validate_auth_config()
