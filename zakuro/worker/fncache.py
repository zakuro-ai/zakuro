"""Load each function once, keyed by the hash of its pickled bytes (opt-in).

The SDK embeds the function in the ``/execute`` body as a ``pickle.loads(bytes)``
reduction (``zakuro.processors._payload``). Unpickling the body through
:func:`loads_payload` sends that call to :func:`cached_loads`, so with the cache
on, a function whose bytes have been seen before is not unpickled again.
Everything else in the body unpickles exactly as ``cloudpickle.loads`` would,
so a body from an older SDK (the function object itself) loads unchanged and is
simply not cached.

The cache is **off** unless ``ZAKURO_FN_CACHE=1``. A cached function keeps one
globals dict, shared by every call that loads the same bytes on this worker:
calls running at the same time on the worker's thread pool, and calls from
other users whose functions pickle to the same bytes. That is what keeps a
loaded model in a module global between calls, as Ray does, and also what lets
two such calls see each other's state. Turn it on only on a worker that serves
one owner, for functions that keep state on purpose.

With the cache on, it holds at most ``ZAKURO_FN_CACHE_SIZE`` functions (default
256) and ``ZAKURO_FN_CACHE_BYTES`` of pickled bytes (default 64 MiB), evicting
the least recently used first. A function pickled larger than 1 MiB (one that
carries its data with it) is loaded on every call and never kept. A change to
a global the function reads changes its bytes, so it is a different entry.
"""

from __future__ import annotations

import hashlib
import io
import os
import pickle
import threading
from collections import OrderedDict
from typing import Any

_DEFAULT_SIZE = 256
_DEFAULT_BYTES = 64 * 1024 * 1024
_MAX_ENTRY_BYTES = 1024 * 1024

_lock = threading.Lock()
_cache: OrderedDict[bytes, tuple[Any, int]] = OrderedDict()
_bytes = 0
_hits = 0
_misses = 0


def _enabled() -> bool:
    return os.environ.get("ZAKURO_FN_CACHE", "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def cached_loads(data: bytes) -> Any:
    """``pickle.loads(data)``, served from the cache when it is on and ``data``
    was seen."""
    global _bytes, _hits, _misses
    if not _enabled():
        return pickle.loads(data)
    key = hashlib.blake2b(data, digest_size=16).digest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            _hits += 1
            return _cache[key][0]
        _misses += 1
    loaded = pickle.loads(data)
    size = len(data)
    budget = _env_int("ZAKURO_FN_CACHE_BYTES", _DEFAULT_BYTES)
    if size > min(_MAX_ENTRY_BYTES, budget):
        return loaded
    capacity = _env_int("ZAKURO_FN_CACHE_SIZE", _DEFAULT_SIZE)
    with _lock:
        if key not in _cache:
            _cache[key] = (loaded, size)
            _bytes += size
        _cache.move_to_end(key)
        while len(_cache) > capacity or _bytes > budget:
            _, (_, evicted) = _cache.popitem(last=False)
            _bytes -= evicted
    return loaded


class _CachingUnpickler(pickle.Unpickler):
    """An unpickler whose ``pickle.loads`` reductions go through the cache."""

    def find_class(self, module: str, name: str) -> Any:
        if name == "loads" and module in ("_pickle", "pickle"):
            return cached_loads
        return super().find_class(module, name)


def loads_payload(payload: bytes) -> Any:
    """Unpickle an ``/execute`` body, loading its function through the cache."""
    return _CachingUnpickler(io.BytesIO(payload)).load()


def cache_info() -> tuple[int, int]:
    """(hits, misses) since the last :func:`clear`."""
    with _lock:
        return _hits, _misses


def clear() -> None:
    """Empty the cache and reset its counters."""
    global _bytes, _hits, _misses
    with _lock:
        _cache.clear()
        _bytes = 0
        _hits = 0
        _misses = 0
