"""HTTP processor using ZakuroClient."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import cloudpickle

from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from zakuro.client import ZakuroClient
    from zakuro.compute import Compute


class HttpProcessor(Processor):
    """HTTP-based processor using ZakuroClient.

    This is the default processor that communicates with Zakuro workers
    over HTTP. It's always available as it has no external dependencies.

    URI schemes:
        - zakuro://host:port (canonical)
        - http://host:port

    Example:
        >>> config = ProcessorConfig.from_uri("zakuro://worker:3960")
        >>> processor = HttpProcessor(config, compute)
        >>> with processor:
        ...     result = processor.execute(func_bytes, args, kwargs)
    """

    priority: ClassVar[int] = 10
    schemes: ClassVar[tuple[str, ...]] = ("zakuro", "http", "https")

    def __init__(self, config: ProcessorConfig, compute: Compute) -> None:
        super().__init__(config, compute)
        self._client: ZakuroClient | None = None

    @classmethod
    def is_available(cls) -> bool:
        """HTTP processor is always available."""
        return True

    def connect(self) -> None:
        """Initialize HTTP client connection."""
        if self._connected:
            return

        from zakuro.client import ZakuroClient

        # Update compute with URI-based host/port
        self._compute.host = self._config.host
        self._compute.port = self._config.port

        self._client = ZakuroClient(self._compute)
        self._connected = True
        self._refuse_if_worker_cannot_meet_the_spec()

    def _refuse_if_worker_cannot_meet_the_spec(self) -> None:
        """Fail loudly when the worker cannot provide what was asked for.

        This path talks to ONE worker directly, so there is no router to match
        a spec against a fleet -- and nothing was checking. `Compute(gpus=1)`
        therefore ran happily on a CPU-only worker and returned a perfectly
        normal-looking answer, which is the worst way to be wrong: the caller
        asked for a GPU, did not get one, and was not told.

        Only GPUs are enforced. CPUs and memory are shared and oversubscribed
        by design, so a shortfall there is scheduling pressure rather than a
        promise the worker cannot keep.
        """
        if self._compute.gpus <= 0:
            return
        try:
            info = self.info()
        except Exception:
            # An unreachable worker is the caller's next error anyway; do not
            # replace it with a confusing one about resources.
            return
        available = (info.get("resources") or {}).get("gpus_total", 0)
        if available < self._compute.gpus:
            raise RuntimeError(
                f"This Compute asks for {self._compute.gpus} GPU(s) but the worker at "
                f"{self._compute.host}:{self._compute.port} reports {available}. "
                "Running here would silently give you a CPU. Route through a broker "
                "(`zakuro.nodes()` and `Compute(uri=node.uri)`) so the request can be "
                "placed on a node that has one."
            )

    def disconnect(self) -> None:
        """Close HTTP client connection."""
        if self._client is not None:
            self._client.close()
            self._client = None
        self._connected = False

    def execute(self, func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Execute function via HTTP worker.

        Args:
            func_bytes: cloudpickle-serialized function
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            Deserialized result from worker

        Raises:
            RuntimeError: If not connected
            httpx.HTTPStatusError: If worker returns error
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        # Deserialize function to repackage with args
        func = cloudpickle.loads(func_bytes)

        # Serialize function and arguments together
        payload = cloudpickle.dumps(
            {
                "func": func,
                "args": args,
                "kwargs": kwargs,
            }
        )

        # Execute via HTTP client
        result_bytes = self._client.execute(payload)

        # Deserialize result
        result = cloudpickle.loads(result_bytes)

        if isinstance(result, Exception):
            raise result

        return result

    def execute_payload(self, payload: bytes, extra_headers: dict[str, str] | None = None) -> bytes:
        """Send a raw cloudpickle request payload via the HTTP worker.

        Returns the raw cloudpickle response bytes (result or exception);
        used by stateful ``@cls`` proxies.
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")
        return self._client.execute(payload, extra_headers=extra_headers)

    def ping(self) -> bool:
        """Check worker health."""
        if self._client is None:
            return False
        return self._client.ping()

    def info(self) -> dict[str, Any]:
        """Get worker information."""
        if self._client is None:
            raise RuntimeError("Processor not connected.")
        return self._client.info()
