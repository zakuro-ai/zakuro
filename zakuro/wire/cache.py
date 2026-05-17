"""Worker-side payload cache for v0.2 state-dict delta encoding (#174 Phase 2).

The wire format (RFC 0001 amendment) carries two new fields on
:class:`zakuro.wire.EnvelopeV2`:

- ``cache_key`` — when set, the worker stores the reconstructed
  ``callable`` bytes under this key after the dispatch completes.
- ``delta_against`` — when set, ``callable`` is a *delta* against the
  bytes previously cached under this key. The worker reconstructs the
  full payload via ``bsdiff4.patch(cached, delta)`` before invoking the
  executor.

This module owns:

* :class:`PayloadCache` — a bounded LRU (by entry count and total bytes)
  keyed by ``cache_key``.
* :func:`apply_delta` — the single chokepoint that reconstructs full
  callable bytes from a cached value + a bsdiff4 delta.
* :class:`CacheMissError` — raised when ``delta_against`` references a
  key that's not in the cache. The handler turns this into a wire-level
  ``WorkerUnavailable { reason: "cache_miss" }`` and the broker retries
  with the full payload.

Tuning knobs (env vars):

* ``ZAKURO_CACHE_MAX_ENTRIES`` — max distinct cache keys. Default 32.
* ``ZAKURO_CACHE_MAX_BYTES`` — total bytes budget. Default 4 GiB.

When ``bsdiff4`` is not installed, :func:`apply_delta` raises
``CacheMissError`` rather than crashing — callers should fall back to
the full-payload path. The cache itself works without the dep.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Final

from zakuro.wire import WireError

# Defaults align with the RFC 0001 amendment §"Cache eviction policy".
DEFAULT_MAX_ENTRIES: Final[int] = 32
DEFAULT_MAX_BYTES: Final[int] = 4 * 1024 * 1024 * 1024  # 4 GiB


class CacheError(WireError):
    """Base for cache + delta-apply failures."""


class CacheMissError(CacheError):
    """An incoming envelope referenced a cache_key that's not present.

    Surfaced to the broker as ``WorkerUnavailable { reason: "cache_miss" }``
    so the broker re-dispatches with the full payload.
    """


class DeltaApplyError(CacheError):
    """``bsdiff4.patch`` raised on the (cached_bytes, delta) pair.

    Usually means the caller computed the delta against a different
    cache version than the worker has. The recovery path is the same
    as :class:`CacheMissError`: the broker retries with the full payload.
    """


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


class PayloadCache:
    """Bounded LRU cache of reconstructed ``callable`` byte blobs.

    Keyed by the ``cache_key`` from :class:`zakuro.wire.EnvelopeV2`.
    Entries are evicted on capacity violation (entry-count *or* total
    bytes); the LRU end is dropped first.

    Single-threaded by design (one cache per worker process). Concurrent
    callers in the same process must serialise via the worker's existing
    thread-pool boundary.
    """

    def __init__(
        self,
        *,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self._max_entries = (
            max_entries
            if max_entries is not None
            else _env_int("ZAKURO_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)
        )
        self._max_bytes = (
            max_bytes
            if max_bytes is not None
            else _env_int("ZAKURO_CACHE_MAX_BYTES", DEFAULT_MAX_BYTES)
        )
        if self._max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        if self._max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self._store: OrderedDict[str, bytes] = OrderedDict()
        self._bytes_in_use: int = 0

    @property
    def entries(self) -> int:
        return len(self._store)

    @property
    def bytes_in_use(self) -> int:
        return self._bytes_in_use

    def get(self, key: str) -> bytes:
        """Look up *key*. Raises :class:`CacheMissError` on miss.

        Promotes the entry to most-recently-used.
        """
        try:
            value = self._store[key]
        except KeyError as exc:
            raise CacheMissError(f"cache miss for key: {key!r}") from exc
        self._store.move_to_end(key)
        return value

    def has(self, key: str) -> bool:
        return key in self._store

    def put(self, key: str, value: bytes) -> None:
        """Store *value* under *key*. Evicts LRU entries on capacity violation."""
        # If the value alone exceeds the bytes budget, refuse — we won't
        # silently drop the entire cache to admit a single oversized blob.
        if len(value) > self._max_bytes:
            raise CacheError(f"value of {len(value)} bytes exceeds max_bytes {self._max_bytes}")

        existing = self._store.pop(key, None)
        if existing is not None:
            self._bytes_in_use -= len(existing)

        self._store[key] = value
        self._bytes_in_use += len(value)

        # Evict LRU until we're under both caps.
        while self._store and (
            len(self._store) > self._max_entries or self._bytes_in_use > self._max_bytes
        ):
            _, evicted = self._store.popitem(last=False)
            self._bytes_in_use -= len(evicted)

    def drop(self, key: str) -> bool:
        """Remove *key* unconditionally. Returns True if a value was present."""
        existing = self._store.pop(key, None)
        if existing is None:
            return False
        self._bytes_in_use -= len(existing)
        return True

    def clear(self) -> None:
        """Empty the cache."""
        self._store.clear()
        self._bytes_in_use = 0


def apply_delta(cached: bytes, delta: bytes) -> bytes:
    """Reconstruct the full payload from a cached value + a bsdiff4 delta.

    The delta is opaque to the wire crate; this module fixes the format
    as **bsdiff4** so the broker and worker agree without a per-message
    discriminator. The RFC 0001 amendment §"Phase 2 follow-up" documents
    the choice; revisit if a tensor-aware diff becomes available.

    Raises :class:`DeltaApplyError` when ``bsdiff4`` isn't installed *or*
    when ``bsdiff4.patch`` raises (most commonly: the delta was computed
    against a different cache version than the worker holds).
    """
    try:
        import bsdiff4
    except ImportError as exc:
        raise DeltaApplyError("bsdiff4 is not installed — install the [worker] extra") from exc
    try:
        return bytes(bsdiff4.patch(cached, delta))
    except Exception as exc:  # bsdiff4 raises various exception types
        raise DeltaApplyError(f"bsdiff4.patch failed: {exc!r}") from exc
