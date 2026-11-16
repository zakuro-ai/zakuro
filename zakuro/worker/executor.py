"""Function execution on the worker's thread pool."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, cast

import cloudpickle


def execute_function(payload: bytes) -> bytes:
    """
    Execute a serialized function and return serialized result.

    This runs on a ``ThreadPoolExecutor`` thread inside the worker process, not in
    a subprocess, so it provides no isolation from the worker itself.

    Args:
        payload: cloudpickle-serialized dict with func, args, kwargs

    Returns:
        cloudpickle-serialized result or exception
    """
    try:
        data = cloudpickle.loads(payload)

        # Handle different action types
        action = data.get("action", "execute")

        if action == "execute" or "func" in data:
            # Standard function execution
            func = data["func"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})

            result = func(*args, **kwargs)
            return cast(bytes, cloudpickle.dumps(result))

        elif action == "create_instance":
            # Class instantiation
            klass = data["klass"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})

            client_id = data.get("instance_id")
            _ensure_capacity(client_id)
            instance = klass(*args, **kwargs)

            # Use client-provided instance_id for broker affinity routing,
            # otherwise auto-generate one
            if client_id:
                instance_id = _put_instance(client_id, instance)
            else:
                instance_id = _store_instance(instance)
            return cast(bytes, cloudpickle.dumps({"instance_id": instance_id}))

        elif action == "call_method":
            # Method call on stored instance
            instance_id = data["instance_id"]
            method_name = data["method"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})

            instance = _get_instance(instance_id)
            method = getattr(instance, method_name)
            result = method(*args, **kwargs)
            return cast(bytes, cloudpickle.dumps(result))

        elif action == "release_instance":
            _cleanup_instance(data["instance_id"])
            return cast(bytes, cloudpickle.dumps({"released": True}))

        else:
            raise ValueError(f"Unknown action: {action}")

    except Exception as e:
        # Return exception to be raised on client
        return cast(bytes, cloudpickle.dumps(e))


# In-memory, node-local, bounded instance store (ZAK-0016).
#
# Per-worker (not per tenant), thread-safe. Bounded by a max count and an idle
# TTL, both read from the environment at use time:
#   ZAKURO_CLS_MAX_INSTANCES  (default 1024)
#   ZAKURO_CLS_IDLE_TTL_S     (default 3600; <= 0 disables the TTL)
# Expired instances are evicted lazily on create/get. Creating past the cap
# (after evicting expired ones) is refused; active instances are never evicted.
_instances: dict[str, Any] = {}
_last_used: dict[str, float] = {}
_instance_counter = 0
_lock = threading.RLock()

DEFAULT_MAX_INSTANCES = 1024
DEFAULT_IDLE_TTL_S = 3600.0


class InstanceStoreFullError(RuntimeError):
    """Raised when the worker's instance store is at capacity."""


class InstanceNotFoundError(ValueError):
    """Raised when an instance id is unknown, expired or evicted."""


def _max_instances() -> int:
    try:
        return int(os.environ.get("ZAKURO_CLS_MAX_INSTANCES", DEFAULT_MAX_INSTANCES))
    except ValueError:
        return DEFAULT_MAX_INSTANCES


def _idle_ttl_s() -> float:
    try:
        return float(os.environ.get("ZAKURO_CLS_IDLE_TTL_S", DEFAULT_IDLE_TTL_S))
    except ValueError:
        return DEFAULT_IDLE_TTL_S


def _evict_expired() -> int:
    """Evict instances idle longer than the TTL. Returns how many were evicted."""
    ttl = _idle_ttl_s()
    if ttl <= 0:
        return 0
    now = time.monotonic()
    with _lock:
        stale = [i for i, t in _last_used.items() if now - t > ttl]
        for instance_id in stale:
            _cleanup_instance(instance_id)
    return len(stale)


def _ensure_capacity(instance_id: str | None = None) -> None:
    """Raise InstanceStoreFullError if a new instance cannot be stored.

    Call before constructing the object so a refused create costs nothing.
    Replacing an existing id does not grow the store and is always allowed.
    """
    with _lock:
        if instance_id is not None and instance_id in _instances:
            return
        if len(_instances) >= _max_instances():
            _evict_expired()
        if len(_instances) >= _max_instances():
            raise InstanceStoreFullError(
                f"Instance store full ({_max_instances()} instances); "
                "release instances or raise ZAKURO_CLS_MAX_INSTANCES"
            )


def _put_instance(instance_id: str, instance: Any) -> str:
    """Store an instance under a given id, enforcing the cap."""
    with _lock:
        _ensure_capacity(instance_id)
        _instances[instance_id] = instance
        _last_used[instance_id] = time.monotonic()
    return instance_id


def _store_instance(instance: Any) -> str:
    """Store an instance under a fresh id and return it."""
    global _instance_counter
    with _lock:
        _instance_counter += 1
        return _put_instance(f"instance_{_instance_counter}", instance)


def _get_instance(instance_id: str) -> Any:
    """Retrieve an instance by ID, refreshing its idle timer."""
    with _lock:
        _evict_expired()
        if instance_id not in _instances:
            raise InstanceNotFoundError(f"Instance not found: {instance_id} (expired or evicted)")
        _last_used[instance_id] = time.monotonic()
        return _instances[instance_id]


def _cleanup_instance(instance_id: str) -> None:
    """Remove an instance from storage (idempotent)."""
    with _lock:
        _instances.pop(instance_id, None)
        _last_used.pop(instance_id, None)
