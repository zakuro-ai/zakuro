"""Function execution in isolated subprocess."""

from __future__ import annotations

from typing import Any

import cloudpickle


def execute_function(payload: bytes) -> bytes:
    """
    Execute a serialized function and return serialized result.

    This runs in a subprocess for isolation.

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
            return cloudpickle.dumps(result)

        elif action == "create_instance":
            # Class instantiation
            klass = data["klass"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})

            instance = klass(*args, **kwargs)

            # Use client-provided instance_id for broker affinity routing,
            # otherwise auto-generate one
            client_id = data.get("instance_id")
            if client_id:
                _instances[client_id] = instance
                instance_id = client_id
            else:
                instance_id = _store_instance(instance)
            return cloudpickle.dumps({"instance_id": instance_id})

        elif action == "call_method":
            # Method call on stored instance
            instance_id = data["instance_id"]
            method_name = data["method"]
            args = data.get("args", ())
            kwargs = data.get("kwargs", {})

            instance = _get_instance(instance_id)
            method = getattr(instance, method_name)
            result = method(*args, **kwargs)
            return cloudpickle.dumps(result)

        else:
            raise ValueError(f"Unknown action: {action}")

    except Exception as e:
        # Return exception to be raised on client
        return cloudpickle.dumps(e)


# Simple in-memory instance storage
# In production, this would be more sophisticated
_instances: dict[str, Any] = {}
_instance_counter = 0


def _store_instance(instance: Any) -> str:
    """Store an instance and return its ID."""
    global _instance_counter
    _instance_counter += 1
    instance_id = f"instance_{_instance_counter}"
    _instances[instance_id] = instance
    return instance_id


def _get_instance(instance_id: str) -> Any:
    """Retrieve an instance by ID."""
    if instance_id not in _instances:
        raise ValueError(f"Instance not found: {instance_id}")
    return _instances[instance_id]


def _cleanup_instance(instance_id: str) -> None:
    """Remove an instance from storage."""
    _instances.pop(instance_id, None)
