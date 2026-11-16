"""Signed wire envelope (ZAKURO_WIRE=v1): HTTP + QUIC gate before cloudpickle (ZAK-0013, #83)."""

from __future__ import annotations

from collections.abc import Iterator

import cloudpickle
import pytest
from fastapi.testclient import TestClient

from zakuro.wire import HmacMismatchError, WireDecodeError, decode_envelope, verify_hmac
from zakuro.wire.keys import (
    WireKeyError,
    derive_payload_hmac_key,
    read_master_key,
    sign_payload,
    wrap_payload,
)
from zakuro.worker import envelope, server
from zakuro.worker.posture import StartupConfigError, validate_wire_config

KEY = b"\x11" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ZAKURO_WIRE", "ZAKURO_HMAC_KEY", "ZAKURO_HMAC_KEY_FILE", "ZAKURO_TENANT_ID"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", KEY.hex())


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(server.app) as c:
        yield c


def _body() -> bytes:
    return cloudpickle.dumps({"func": lambda a, b: a + b, "args": (2, 3), "kwargs": {}})


def test_unset_is_passthrough_and_wrap_is_identity() -> None:
    raw = b"anything"
    assert envelope.unwrap_payload(raw)[0] is raw
    assert wrap_payload(raw) is raw


def test_hkdf_matches_rfc5869_construction() -> None:
    import hashlib
    import hmac

    prk = hmac.new(b"zakuro-wire-v1", KEY, hashlib.sha256).digest()
    expected = hmac.new(prk, b"payload:t1\x01", hashlib.sha256).digest()
    assert derive_payload_hmac_key(master_key=KEY, tenant_id="t1") == expected
    assert derive_payload_hmac_key(master_key=KEY, tenant_id="t2") != expected


def test_roundtrip_and_tenant_isolation(strict: None) -> None:
    raw = sign_payload(b"inner", master_key=KEY, tenant_id="acme", job_id="j1")
    assert envelope.unwrap_payload(raw)[0] == b"inner"
    env = decode_envelope(raw)
    with pytest.raises(HmacMismatchError):
        verify_hmac(env, hmac_key=derive_payload_hmac_key(master_key=KEY, tenant_id="other"))


def test_unsigned_tampered_and_wrong_key_rejected(
    strict: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = sign_payload(b"inner-callable", master_key=KEY)
    tampered = bytearray(signed)
    tampered[-50] ^= 0xFF
    other = sign_payload(b"inner-callable", master_key=b"\x22" * 32)
    for bad in (_body(), b"", bytes(tampered), other, signed + b"x"):
        with pytest.raises(envelope.EnvelopeRejectedError):
            envelope.unwrap_payload(bad)[0]


def test_decode_rejects_garbage() -> None:
    with pytest.raises(WireDecodeError):
        decode_envelope(b"")
    with pytest.raises(WireDecodeError):
        decode_envelope(b"\x00\x05ab")


def test_key_loading(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with pytest.raises(WireKeyError):
        read_master_key()
    kf = tmp_path / "k"
    kf.write_bytes(KEY.hex().encode())
    monkeypatch.setenv("ZAKURO_HMAC_KEY_FILE", str(kf))
    assert read_master_key() == KEY.hex().encode()
    monkeypatch.setenv("ZAKURO_HMAC_KEY", KEY.hex())
    with pytest.raises(WireKeyError, match="pick one"):
        read_master_key()
    monkeypatch.delenv("ZAKURO_HMAC_KEY_FILE")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", "zz")
    with pytest.raises(WireKeyError):
        read_master_key()


def test_error_text_never_contains_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_HMAC_KEY", "not-hex-" + KEY.hex())
    with pytest.raises(WireKeyError) as ei:
        read_master_key()
    assert KEY.hex() not in str(ei.value)


def test_startup_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    validate_wire_config()  # unset -> fine
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    with pytest.raises(StartupConfigError):
        validate_wire_config()
    monkeypatch.setenv("ZAKURO_HMAC_KEY", KEY.hex())
    validate_wire_config()


def test_http_unset_still_runs_unsigned(client: TestClient) -> None:
    r = client.post("/execute", content=_body())
    assert r.status_code == 200 and cloudpickle.loads(r.content) == 5


def test_http_signed_executes(client: TestClient, strict: None) -> None:
    r = client.post("/execute", content=wrap_payload(_body()))
    assert r.status_code == 200 and cloudpickle.loads(r.content) == 5


def test_http_unsigned_and_tampered_get_401_before_unpickle(
    client: TestClient, strict: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("cloudpickle.loads reached before verification")

    monkeypatch.setattr(server.cloudpickle, "loads", boom)
    from zakuro.worker import executor as ex

    monkeypatch.setattr(ex.cloudpickle, "loads", boom)
    tampered = bytearray(wrap_payload(_body()))
    tampered[-50] ^= 0xFF
    for bad in (_body(), bytes(tampered)):
        r = client.post("/execute", content=bad)
        assert r.status_code == 401 and r.content == b""


def test_quic_payload_gate(strict: None, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("aioquic")
    from zakuro.worker import quic_server

    ok_status, ok_body = quic_server._execute_payload(wrap_payload(_body()))
    assert ok_status == quic_server.STAT_OK and cloudpickle.loads(ok_body) == 5

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("cloudpickle.loads reached before verification")

    monkeypatch.setattr(quic_server.cloudpickle, "loads", boom)
    tampered = bytearray(wrap_payload(_body()))
    tampered[-50] ^= 0xFF
    for bad in (_body(), bytes(tampered)):
        status, msg = quic_server._execute_payload(bad)
        assert status == quic_server.STAT_PROTOCOL_ERROR and b"unauthorized" in msg


def test_quic_end_to_end_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("aioquic")
    import zakuro as zk

    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", KEY.hex())

    @zk.fn
    def add(a: int, b: int) -> int:
        return a + b

    with zk.Worker.spawn(name="quic-wire", transport="quic") as worker:
        assert add.to(worker.compute(verify=False))(3, 4) == 7
        # an unsigned client is refused
        monkeypatch.setenv("ZAKURO_WIRE", "")
        with pytest.raises(RuntimeError, match="unauthorized"):
            add.to(worker.compute(verify=False))(3, 4)
