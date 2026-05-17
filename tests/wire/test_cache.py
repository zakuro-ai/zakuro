"""Unit tests for the v0.2 payload cache + delta-apply (#174 Phase 2 substrate)."""

from __future__ import annotations

import pytest

from zakuro.wire.cache import (
    CacheError,
    CacheMissError,
    DeltaApplyError,
    PayloadCache,
    apply_delta,
)

# ---- PayloadCache contract --------------------------------------------------


def test_put_then_get_round_trips() -> None:
    cache = PayloadCache(max_entries=4, max_bytes=1024)
    cache.put("epoch-1", b"state-dict-bytes")
    assert cache.get("epoch-1") == b"state-dict-bytes"
    assert cache.has("epoch-1")


def test_get_raises_on_miss() -> None:
    cache = PayloadCache(max_entries=4, max_bytes=1024)
    with pytest.raises(CacheMissError, match="epoch-0"):
        cache.get("epoch-0")


def test_lru_evicts_oldest_when_entries_full() -> None:
    cache = PayloadCache(max_entries=2, max_bytes=1024)
    cache.put("a", b"AAA")
    cache.put("b", b"BBB")
    cache.put("c", b"CCC")  # evicts "a"
    assert not cache.has("a")
    assert cache.has("b")
    assert cache.has("c")
    assert cache.entries == 2


def test_lru_evicts_when_bytes_budget_exceeded() -> None:
    cache = PayloadCache(max_entries=10, max_bytes=10)
    cache.put("a", b"AAAA")  # 4 bytes
    cache.put("b", b"BBBB")  # 4 bytes; total 8 OK
    cache.put("c", b"CCCC")  # 4 bytes → total 12 > 10 → evict "a"
    assert not cache.has("a")
    assert cache.bytes_in_use == 8


def test_get_promotes_entry_to_mru() -> None:
    cache = PayloadCache(max_entries=2, max_bytes=1024)
    cache.put("a", b"AAA")
    cache.put("b", b"BBB")
    # Touch "a" → now "b" is LRU
    cache.get("a")
    cache.put("c", b"CCC")  # should evict "b" not "a"
    assert cache.has("a")
    assert not cache.has("b")


def test_put_same_key_updates_value_not_entry_count() -> None:
    cache = PayloadCache(max_entries=4, max_bytes=1024)
    cache.put("a", b"v1")
    cache.put("a", b"v2")
    assert cache.entries == 1
    assert cache.get("a") == b"v2"
    assert cache.bytes_in_use == 2


def test_oversized_single_value_rejected() -> None:
    cache = PayloadCache(max_entries=10, max_bytes=10)
    with pytest.raises(CacheError, match="exceeds max_bytes"):
        cache.put("big", b"x" * 100)


def test_drop_removes_entry() -> None:
    cache = PayloadCache(max_entries=4, max_bytes=1024)
    cache.put("a", b"AAA")
    assert cache.drop("a") is True
    assert cache.drop("a") is False
    assert not cache.has("a")
    assert cache.bytes_in_use == 0


def test_clear_resets_everything() -> None:
    cache = PayloadCache(max_entries=4, max_bytes=1024)
    cache.put("a", b"AAA")
    cache.put("b", b"BBB")
    cache.clear()
    assert cache.entries == 0
    assert cache.bytes_in_use == 0


def test_env_vars_drive_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_CACHE_MAX_ENTRIES", "3")
    monkeypatch.setenv("ZAKURO_CACHE_MAX_BYTES", "100")
    cache = PayloadCache()
    cache.put("a", b"AA")
    cache.put("b", b"BB")
    cache.put("c", b"CC")
    cache.put("d", b"DD")  # exceeds max_entries=3 → evicts "a"
    assert not cache.has("a")
    assert cache.entries == 3


def test_env_var_garbage_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAKURO_CACHE_MAX_ENTRIES", "not-a-number")
    cache = PayloadCache()
    # Falls back to DEFAULT_MAX_ENTRIES = 32
    assert cache._max_entries == 32  # noqa: SLF001 — test verifies internal


def test_constructor_rejects_non_positive_caps() -> None:
    with pytest.raises(ValueError, match="max_entries"):
        PayloadCache(max_entries=0, max_bytes=1024)
    with pytest.raises(ValueError, match="max_bytes"):
        PayloadCache(max_entries=4, max_bytes=0)


# ---- delta apply ------------------------------------------------------------


def test_apply_delta_round_trip() -> None:
    bsdiff4 = pytest.importorskip("bsdiff4")
    cached = b"a" * 1000 + b"b" * 1000
    new = b"a" * 1000 + b"c" * 1000  # one section differs
    delta = bsdiff4.diff(cached, new)
    reconstructed = apply_delta(cached, delta)
    assert reconstructed == new


def test_apply_delta_is_smaller_than_full_payload() -> None:
    bsdiff4 = pytest.importorskip("bsdiff4")
    # 100 KB payload with a tiny change
    cached = b"x" * 100_000
    new = b"x" * 99_999 + b"y"
    delta = bsdiff4.diff(cached, new)
    assert len(delta) < len(new) // 5, f"delta {len(delta)} should be << {len(new) // 5}"
    assert apply_delta(cached, delta) == new


def test_apply_delta_truncated_input_raises() -> None:
    """A malformed delta blob raises DeltaApplyError, not a silent corruption."""
    pytest.importorskip("bsdiff4")
    with pytest.raises(DeltaApplyError):
        apply_delta(b"some-cache", b"not-a-valid-bsdiff4-delta-blob")


# NOTE: bsdiff4 is lenient by design — applying a valid delta against a
# *different* base produces garbage rather than raising. That's fine; the
# wire-format HMAC (RFC 0002) catches it: a reconstructed payload that
# wasn't built from the expected base produces a different HMAC than the
# broker computed, so safe_loads rejects with HmacMismatchError before
# the bytes reach cloudpickle. The cache layer here is intentionally not
# the security boundary.
