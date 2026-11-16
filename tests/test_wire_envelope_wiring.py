"""Phase-2 wiring tests: /execute decodes the postcard envelope (#117)."""

from __future__ import annotations

import cloudpickle
import pytest
from fastapi.testclient import TestClient

from zakuro.worker import envelope as wire_envelope


@pytest.fixture
def master_key() -> bytes:
    return b"\x11" * 32


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_WIRE", raising=False)
    monkeypatch.delenv("ZAKURO_HMAC_KEY", raising=False)
    monkeypatch.delenv("ZAKURO_HMAC_KEY_FILE", raising=False)


def _client() -> TestClient:
    # Import lazily so the env-reset fixture has applied first. Use the
    # context-manager form so FastAPI fires the startup event (which
    # initialises the worker thread pool). Without `with`, /execute
    # returns 503 because the executor is still None.
    from zakuro.worker.server import app

    return TestClient(app)


def _execute(client: TestClient, body: bytes) -> Any:
    return client.post("/execute", content=body)


# ---- unwrap_payload contract --------------------------------------------------


def test_unwrap_returns_raw_when_wire_unset() -> None:
    raw = b"some-cloudpickle-bytes"
    inner, env = wire_envelope.unwrap_payload(raw)
    assert inner is raw  # identity passthrough — no allocation
    assert env is None


def test_unwrap_decodes_strict_mode(monkeypatch: pytest.MonkeyPatch, master_key: bytes) -> None:
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", master_key.hex())
    raw = wire_envelope.build_envelope_payload(
        callable_bytes=b"cloudpickle-bytes-here",
        master_key=master_key,
    )
    inner, env = wire_envelope.unwrap_payload(raw)
    assert inner == b"cloudpickle-bytes-here"
    assert env is not None
    assert env.tenant_id == "tenant-acme"


def test_unwrap_rejects_tampered_envelope(
    monkeypatch: pytest.MonkeyPatch, master_key: bytes
) -> None:
    from zakuro.wire import HmacMismatchError

    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", master_key.hex())
    raw = wire_envelope.build_envelope_payload(
        callable_bytes=b"original",
        master_key=master_key,
    )
    tampered = bytearray(raw)
    # Flip a byte inside the callable region (postcard layout puts the
    # callable bytes after the length-prefixed strings + version byte).
    tampered[-50] ^= 0xFF
    with pytest.raises((HmacMismatchError, wire_envelope.WireError)):
        wire_envelope.unwrap_payload(bytes(tampered))


def test_unwrap_fails_closed_without_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    with pytest.raises(wire_envelope.EnvelopeRejected, match="master key"):
        wire_envelope.unwrap_payload(b"\x00")


def test_unwrap_refuses_both_key_sources(
    monkeypatch: pytest.MonkeyPatch, master_key: bytes, tmp_path: Any
) -> None:
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", master_key.hex())
    key_file = tmp_path / "k"
    key_file.write_bytes(master_key)
    monkeypatch.setenv("ZAKURO_HMAC_KEY_FILE", str(key_file))
    with pytest.raises(wire_envelope.EnvelopeRejected, match="pick one"):
        wire_envelope.unwrap_payload(b"\x00")


# ---- /execute end-to-end ------------------------------------------------------


def _build_cloudpickle_request(value: int) -> bytes:
    def f(x: int) -> int:
        return x * 2

    return cloudpickle.dumps({"action": "execute", "func": f, "args": (value,)})


def test_execute_legacy_mode_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """ZAKURO_WIRE unset: behaviour is identical to pre-#117."""
    monkeypatch.delenv("ZAKURO_WIRE", raising=False)
    payload = _build_cloudpickle_request(21)
    with _client() as c:
        r = c.post("/execute", content=payload)
    assert r.status_code == 200
    result = cloudpickle.loads(r.content)
    assert result == 42


def test_execute_strict_mode_round_trips(
    monkeypatch: pytest.MonkeyPatch, master_key: bytes
) -> None:
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", master_key.hex())
    inner = _build_cloudpickle_request(7)
    env_bytes = wire_envelope.build_envelope_payload(
        callable_bytes=inner,
        master_key=master_key,
    )
    with _client() as c:
        r = c.post("/execute", content=env_bytes)
    assert r.status_code == 200
    result = cloudpickle.loads(r.content)
    assert result == 14


def test_execute_strict_mode_rejects_bad_envelope(
    monkeypatch: pytest.MonkeyPatch, master_key: bytes
) -> None:
    """A naked cloudpickle blob in strict mode returns 401, never reaches cloudpickle.loads."""
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", master_key.hex())
    naked = _build_cloudpickle_request(7)
    with _client() as c:
        r = c.post("/execute", content=naked)
    assert r.status_code == 401
    assert r.content == b""


def test_execute_strict_mode_rejects_wrong_key(
    monkeypatch: pytest.MonkeyPatch, master_key: bytes
) -> None:
    """An envelope signed with key A rejected by worker holding key B."""
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", (b"\x22" * 32).hex())
    inner = _build_cloudpickle_request(7)
    env_bytes = wire_envelope.build_envelope_payload(
        callable_bytes=inner,
        master_key=master_key,  # NOT the key the worker is configured with
    )
    with _client() as c:
        r = c.post("/execute", content=env_bytes)
    assert r.status_code == 401


# ---- import for tmp_path fixture typing --------------------------------------
from typing import Any  # noqa: E402  (kept at bottom for forward-ref clarity)
