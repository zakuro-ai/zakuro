"""Wire envelope v2 (ZAKURO_WIRE=v2): framed MAC, freshness, replay (ZAK-0020, #103)."""

from __future__ import annotations

import threading
import time

import pytest

from zakuro.wire import (
    HmacMismatchError,
    WireDecodeError,
    compute_hmac_v2,
    decode_envelope_v2,
    verify_hmac,
    verify_hmac_v2,
)
from zakuro.wire.keys import (
    derive_payload_hmac_key,
    derive_payload_hmac_key_v2,
    sign_payload,
    sign_payload_v2,
    wrap_payload,
)
from zakuro.worker import envelope
from zakuro.worker.envelope import EnvelopeRejectedError, ReplayCache

KEY = b"\x11" * 32

# Fixed vectors shared with docs/adr/0020-wire-envelope-v2.md and zakuro-ai/zc#415.
VEC_TENANT_KEY = "b38c960e38b07c76a04fcfad14e1d433c43127dea5dc847eb109be0440e59e32"
VEC_MAC = "415981780bfa27bbdb5b06422fb7534290d98b31594d6ac07def49cb827349f2"
VEC_ENVELOPE = (
    "01056a6f622d310461636d658080b3c19c330e63616c6c61626c652d62797465730a"
    "617267732d6279746573415981780bfa27bbdb5b06422fb7534290d98b31594d6ac0"
    "7def49cb827349f20000803f8008003c"
)
VEC_ISSUED_MS = 1760000000000


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ZAKURO_WIRE",
        "ZAKURO_HMAC_KEY",
        "ZAKURO_HMAC_KEY_FILE",
        "ZAKURO_TENANT_ID",
        "ZAKURO_WIRE_MAX_SKEW_S",
        "ZAKURO_WIRE_ACCEPT_V1",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ZAKURO_HMAC_KEY", KEY.hex())
    envelope._replay_cache.clear()


@pytest.fixture
def v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_WIRE", "v2")


def test_fixed_vectors() -> None:
    key = derive_payload_hmac_key_v2(master_key=KEY, tenant_id="acme")
    assert key.hex() == VEC_TENANT_KEY
    mac = compute_hmac_v2(
        hmac_key=key,
        tenant_id="acme",
        job_id="job-1",
        issued_at_ms=VEC_ISSUED_MS,
        callable_bytes=b"callable-bytes",
        args=b"args-bytes",
    )
    assert mac.hex() == VEC_MAC
    raw = sign_payload_v2(
        b"callable-bytes",
        master_key=KEY,
        args=b"args-bytes",
        job_id="job-1",
        tenant_id="acme",
        issued_at_ms=VEC_ISSUED_MS,
    )
    assert raw.hex() == VEC_ENVELOPE
    env = decode_envelope_v2(raw)
    assert env.issued_at_ms == VEC_ISSUED_MS and env.job_id == "job-1"
    verify_hmac_v2(env, hmac_key=key)


def test_v1_and_v2_keys_differ() -> None:
    assert derive_payload_hmac_key(master_key=KEY, tenant_id="t") != derive_payload_hmac_key_v2(
        master_key=KEY, tenant_id="t"
    )


def test_field_shifting_verifies_under_v1_but_not_v2() -> None:
    # v1 MAC input is unframed callable||args||job_id: moving a byte between fields keeps it.
    v1_raw = sign_payload(b"AAAB", master_key=KEY, args=b"CC", job_id="j")
    from zakuro.wire import decode_envelope, encode_envelope

    e1 = decode_envelope(v1_raw)
    shifted = type(e1)(
        version=e1.version,
        job_id=e1.job_id,
        tenant_id=e1.tenant_id,
        callable=b"AAA",
        args=b"BCC",
        hmac=e1.hmac,
    )
    verify_hmac(shifted, hmac_key=derive_payload_hmac_key(master_key=KEY, tenant_id="default"))
    encode_envelope(shifted)

    e2 = decode_envelope_v2(sign_payload_v2(b"AAAB", master_key=KEY, args=b"CC", job_id="j"))
    moved = type(e2)(
        job_id=e2.job_id,
        tenant_id=e2.tenant_id,
        issued_at_ms=e2.issued_at_ms,
        callable=b"AAA",
        args=b"BCC",
        hmac=e2.hmac,
    )
    with pytest.raises(HmacMismatchError):
        verify_hmac_v2(
            moved, hmac_key=derive_payload_hmac_key_v2(master_key=KEY, tenant_id="default")
        )


def test_v2_roundtrip_and_replay_rejected(v2: None) -> None:
    raw = wrap_payload(b"inner")
    assert envelope.unwrap_payload(raw)[0] == b"inner"
    with pytest.raises(EnvelopeRejectedError):
        envelope.unwrap_payload(raw)[0]
    assert envelope.unwrap_payload(wrap_payload(b"inner"))[0] == b"inner"  # fresh job_id


def test_replay_scoped_per_tenant(v2: None) -> None:
    a = sign_payload_v2(b"x", master_key=KEY, job_id="same", tenant_id="a")
    b = sign_payload_v2(b"x", master_key=KEY, job_id="same", tenant_id="b")
    assert envelope.unwrap_payload(a)[0] == b"x"
    assert envelope.unwrap_payload(b)[0] == b"x"


def test_stale_and_future_rejected(v2: None, monkeypatch: pytest.MonkeyPatch) -> None:
    now_ms = int(time.time() * 1000)
    for delta in (-400_000, 400_000):
        raw = sign_payload_v2(b"x", master_key=KEY, issued_at_ms=now_ms + delta)
        with pytest.raises(EnvelopeRejectedError):
            envelope.unwrap_payload(raw)[0]
    ok = sign_payload_v2(b"x", master_key=KEY, issued_at_ms=now_ms - 100_000)
    assert envelope.unwrap_payload(ok)[0] == b"x"
    monkeypatch.setenv("ZAKURO_WIRE_MAX_SKEW_S", "10")
    old = sign_payload_v2(b"x", master_key=KEY, issued_at_ms=now_ms - 100_000)
    with pytest.raises(EnvelopeRejectedError):
        envelope.unwrap_payload(old)[0]


def test_tampered_and_wrong_key_rejected(v2: None) -> None:
    raw = bytearray(sign_payload_v2(b"inner", master_key=KEY))
    raw[-50] ^= 0xFF
    other = sign_payload_v2(b"inner", master_key=b"\x22" * 32)
    for bad in (bytes(raw), other, b"", b"\x07abc"):
        with pytest.raises(EnvelopeRejectedError):
            envelope.unwrap_payload(bad)[0]


def test_unauthenticated_does_not_fill_replay_cache(v2: None) -> None:
    raw = bytearray(sign_payload_v2(b"inner", master_key=KEY, job_id="jx"))
    raw[-50] ^= 0xFF
    with pytest.raises(EnvelopeRejectedError):
        envelope.unwrap_payload(bytes(raw))[0]
    assert (
        envelope.unwrap_payload(sign_payload_v2(b"inner", master_key=KEY, job_id="jx"))[0]
        == b"inner"
    )


def test_v2_worker_accepts_v1_by_default_and_not_when_disabled(
    v2: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = sign_payload(b"legacy", master_key=KEY)
    assert envelope.unwrap_payload(v1)[0] == b"legacy"
    monkeypatch.setenv("ZAKURO_WIRE_ACCEPT_V1", "0")
    with pytest.raises(EnvelopeRejectedError):
        envelope.unwrap_payload(v1)[0]
    assert envelope.unwrap_payload(wrap_payload(b"new"))[0] == b"new"


def test_v1_worker_rejects_v2_and_v1_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    with pytest.raises(EnvelopeRejectedError):
        envelope.unwrap_payload(sign_payload_v2(b"x", master_key=KEY))[0]
    v1 = wrap_payload(b"x")
    assert envelope.unwrap_payload(v1)[0] == b"x"
    assert envelope.unwrap_payload(v1)[0] == b"x"  # v1 has no replay protection


def test_client_v2_emits_v2_version_byte(v2: None) -> None:
    assert wrap_payload(b"x")[0] == 1


def test_decode_v2_rejects_garbage() -> None:
    with pytest.raises(WireDecodeError):
        decode_envelope_v2(b"")
    with pytest.raises(WireDecodeError):
        decode_envelope_v2(b"\x01\x05ab")
    with pytest.raises(WireDecodeError):
        decode_envelope_v2(bytes.fromhex(VEC_ENVELOPE) + b"x")


def test_replay_cache_bounded_expiring_threadsafe() -> None:
    c = ReplayCache(cap=3)
    for i in range(5):
        assert c.check_and_add(("t", str(i)), now=0, ttl=10)
    assert len(c._seen) == 3
    assert c.check_and_add(("t", "0"), now=0, ttl=10)  # evicted, forgotten
    assert c.check_and_add(("t", "4"), now=11, ttl=10)  # expired

    c2 = ReplayCache()
    wins: list[bool] = []

    def hit() -> None:
        wins.append(c2.check_and_add(("t", "same"), now=0, ttl=10))

    threads = [threading.Thread(target=hit) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
