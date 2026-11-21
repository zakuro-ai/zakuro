"""Load each function once, keyed by the hash of its pickled bytes.

The SDK embeds the function in the ``/execute`` body as a ``pickle.loads(bytes)``
reduction (``zakuro.processors._payload``). Unpickling the body through
:func:`loads_payload` sends that call to :func:`cached_loads`, so a function
whose bytes have been seen before is not unpickled again. Everything else in
the body unpickles exactly as ``cloudpickle.loads`` would, so a body from an
older SDK (the function object itself) loads unchanged and is simply not cached.

A cached function keeps one globals dict, so state a function keeps in its own
module globals now lasts between calls on the same worker, as it does in Ray.
``ZAKURO_FN_CACHE=0`` turns the cache off (a fresh load per call, as before);
``ZAKURO_FN_CACHE_SIZE`` bounds it (default 256 functions, least recently used
evicted first). A change to a global the function reads changes its bytes, so
it is a different entry.
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

_lock = threading.Lock()
_cache: OrderedDict[bytes, Any] = OrderedDict()
_hits = 0
_misses = 0


def _enabled() -> bool:
    return os.environ.get("ZAKURO_FN_CACHE", "1").strip() != "0"


def _capacity() -> int:
    try:
        return max(1, int(os.environ.get("ZAKURO_FN_CACHE_SIZE", _DEFAULT_SIZE)))
    except ValueError:
        return _DEFAULT_SIZE


def cached_loads(data: bytes) -> Any:
    """``pickle.loads(data)``, served from the cache when ``data`` was seen."""
    global _hits, _misses
    if not _enabled():
        return pickle.loads(data)
    key = hashlib.blake2b(data, digest_size=16).digest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            _hits += 1
            return _cache[key]
        _misses += 1
    loaded = pickle.loads(data)
    with _lock:
        _cache[key] = loaded
        _cache.move_to_end(key)
        while len(_cache) > _capacity():
            _cache.popitem(last=False)
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
    global _hits, _misses
    with _lock:
        _cache.clear()
        _hits = 0
        _misses = 0
