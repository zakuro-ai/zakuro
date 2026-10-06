"""Remote proxy for class instances."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Generic, TypeVar

import cloudpickle

from zakuro.compute import Compute

T = TypeVar("T")

logger = logging.getLogger(__name__)


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
        self._instance_id: str | None = None
        self._closed = False

        # Create remote instance
        self._create_remote_instance()

    def _resolved_uri(self) -> str:
        """Resolve the worker URI the same way Fn dispatch does, so the proxy
        uses the worker's own transport (HTTP, QUIC, broker, …) rather than
        assuming HTTP."""
        uri = self._compute.uri
        if uri is None and self._compute.host is not None:
            uri = f"zakuro://{self._compute.host}:{self._compute.port}"
        if uri is None:
            raise RuntimeError("RemoteProxy requires a resolved compute target (uri or host).")
        return uri

    def _send(self, payload: bytes, action: str, instance_id: str | None) -> bytes:
        """Send a request payload over the worker's transport, returning the raw
        cloudpickle response bytes."""
        from zakuro.processors.registry import get_processor

        with get_processor(self._resolved_uri(), self._compute) as processor:
            return processor.execute_payload(
                payload,
                extra_headers={
                    "X-Zakuro-Instance-Action": action,
                    "X-Zakuro-Instance-Id": instance_id or "",
                },
            )

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

        result_bytes = self._send(payload, "create_instance", instance_id)
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

        result_bytes = self._send(payload, "call_method", self._instance_id)
        result = cloudpickle.loads(result_bytes)

        if isinstance(result, Exception):
            raise result

        return result

    def close(self) -> None:
        """Close the proxy: run the class's own ``close()`` (if any), then release.

        If the remote class defines a callable ``close``, it is called first on
        the remote instance (preserving pre-existing behaviour). The instance is
        then released (``release_instance``), best-effort: a missing/expired
        instance or a transport error is logged at debug level and swallowed.
        If the user's ``close`` raised, the instance is still released and the
        original exception is re-raised afterwards.

        Idempotent: only the first call does anything. There is deliberately no
        ``__del__`` finalizer (network I/O at garbage-collection time is
        unreliable); un-closed instances are reclaimed by the worker's idle TTL.
        """
        if self._closed:
            return
        self._closed = True
        instance_id = self._instance_id
        if instance_id is None:
            return
        user_error: BaseException | None = None
        if callable(getattr(self._klass, "close", None)):
            try:
                self._call_remote_method("close", (), {})
            except Exception as exc:
                user_error = exc
        try:
            payload = cloudpickle.dumps({"action": "release_instance", "instance_id": instance_id})
            result = cloudpickle.loads(self._send(payload, "release_instance", instance_id))
            if isinstance(result, Exception):
                logger.debug("release_instance %s failed: %s", instance_id, result)
        except Exception as exc:  # best-effort: release never raises
            logger.debug("release_instance %s failed: %s", instance_id, exc)
        if user_error is not None:
            raise user_error

    def __enter__(self) -> RemoteProxy[T]:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"RemoteProxy({self._klass.__name__}, compute={self._compute})"
