"""Remote proxy for class instances."""

from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

import cloudpickle

from zakuro.client import ZakuroClient
from zakuro.compute import Compute

T = TypeVar("T")


class RemoteProxy(Generic[T]):
    """
    Proxy for a remotely instantiated class.

    All method calls are forwarded to the remote instance.

    Example:
        >>> @cls
        ... class Model:
        ...     def predict(self, x):
        ...         return x * 2
        >>>
        >>> remote_model = Model.to(Compute())()
        >>> result = remote_model.predict(5)  # Executed remotely
    """

    def __init__(
        self,
        klass: type[T],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        compute: Compute,
    ) -> None:
        self._klass = klass
        self._args = args
        self._kwargs = kwargs
        self._compute = compute
        self._client = ZakuroClient(compute)
        self._instance_id: str | None = None

        # Create remote instance
        self._create_remote_instance()

    def _create_remote_instance(self) -> None:
        """Create the instance on the remote worker."""
        # Generate instance ID client-side so the broker can track affinity
        instance_id = f"instance_{uuid.uuid4().hex[:12]}"

        payload = cloudpickle.dumps(
            {
                "action": "create_instance",
                "instance_id": instance_id,
                "klass": self._klass,
                "args": self._args,
                "kwargs": self._kwargs,
            }
        )

        result_bytes = self._client.execute(
            payload,
            extra_headers={
                "X-Zakuro-Instance-Action": "create_instance",
                "X-Zakuro-Instance-Id": instance_id,
            },
        )
        result = cloudpickle.loads(result_bytes)

        if isinstance(result, Exception):
            raise result

        self._instance_id = result.get("instance_id")

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access to remote instance."""
        if name.startswith("_"):
            raise AttributeError(name)

        def method_proxy(*args: Any, **kwargs: Any) -> Any:
            return self._call_remote_method(name, args, kwargs)

        return method_proxy

    def _call_remote_method(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Call a method on the remote instance."""
        payload = cloudpickle.dumps(
            {
                "action": "call_method",
                "instance_id": self._instance_id,
                "method": method_name,
                "args": args,
                "kwargs": kwargs,
            }
        )

        result_bytes = self._client.execute(
            payload,
            extra_headers={
                "X-Zakuro-Instance-Action": "call_method",
                "X-Zakuro-Instance-Id": self._instance_id,
            },
        )
        result = cloudpickle.loads(result_bytes)

        if isinstance(result, Exception):
            raise result

        return result

    def __repr__(self) -> str:
        return f"RemoteProxy({self._klass.__name__}, compute={self._compute})"
