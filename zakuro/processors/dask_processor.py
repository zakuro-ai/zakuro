"""Dask processor for distributed computing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import cloudpickle

from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from distributed import Client

    from zakuro.compute import Compute


class DaskProcessor(Processor):
    """Dask-based processor for distributed computing.

    Uses Dask's distributed Client for task submission to a scheduler.

    URI schemes:
        - dask://host:port (canonical)
        - tcp://host:port

    Example:
        >>> config = ProcessorConfig.from_uri("dask://scheduler:8786")
        >>> processor = DaskProcessor(config, compute)
        >>> with processor:
        ...     result = processor.execute(func_bytes, args, kwargs)
    """

    priority: ClassVar[int] = 40
    schemes: ClassVar[tuple[str, ...]] = ("dask", "tcp")

    def __init__(self, config: ProcessorConfig, compute: Compute) -> None:
        super().__init__(config, compute)
        self._client: Client | None = None

    @classmethod
    def is_available(cls) -> bool:
        """Check if Dask distributed is installed."""
        try:
            from distributed import Client  # noqa: F401

            return True
        except ImportError:
            return False

    def connect(self) -> None:
        """Connect to Dask scheduler."""
        if self._connected:
            return

        from distributed import Client

        # Dask uses tcp:// scheme internally
        address = f"tcp://{self._config.host}:{self._config.port}"

        self._client = Client(address)
        self._connected = True

    def disconnect(self) -> None:
        """Disconnect from Dask scheduler."""
        if self._client is not None:
            self._client.close()
            self._client = None
        self._connected = False

    def execute(self, func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Execute function on Dask cluster.

        Args:
            func_bytes: cloudpickle-serialized function
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            Result from Dask execution

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        def _execute_task(
            serialized_func: bytes, task_args: tuple[Any, ...], task_kwargs: dict[str, Any]
        ) -> Any:
            func = cloudpickle.loads(serialized_func)
            return func(*task_args, **task_kwargs)

        # Submit task and get result
        future = self._client.submit(
            _execute_task,
            func_bytes,
            args,
            kwargs,
            resources=self._build_dask_resources(),
        )

        return future.result()

    def _build_dask_resources(self) -> dict[str, Any]:
        """Build Dask resource constraints.

        Note: Dask resource management is primarily at the worker level,
        but we can pass resource hints for scheduling.
        """
        resources: dict[str, Any] = {}

        # Dask uses custom resource names that must be configured on workers
        # These are hints for the scheduler if workers advertise these resources
        if self._compute.gpus > 0:
            resources["GPU"] = self._compute.gpus

        return resources

    def submit_batch(
        self,
        func_bytes: bytes,
        batch_args: list[tuple[Any, ...]],
        batch_kwargs: list[dict[str, Any]] | None = None,
    ) -> list[Any]:
        """Submit multiple tasks in parallel.

        Args:
            func_bytes: cloudpickle-serialized function
            batch_args: List of args tuples for each invocation
            batch_kwargs: Optional list of kwargs dicts

        Returns:
            List of results in same order as inputs
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        if batch_kwargs is None:
            batch_kwargs = [{} for _ in batch_args]

        def _execute_task(
            serialized_func: bytes, task_args: tuple[Any, ...], task_kwargs: dict[str, Any]
        ) -> Any:
            func = cloudpickle.loads(serialized_func)
            return func(*task_args, **task_kwargs)

        resources = self._build_dask_resources()

        # Submit all tasks
        futures = [
            self._client.submit(
                _execute_task,
                func_bytes,
                args,
                kwargs,
                resources=resources,
            )
            for args, kwargs in zip(batch_args, batch_kwargs, strict=True)
        ]

        # Gather all results
        return self._client.gather(futures)

    def map(
        self,
        func_bytes: bytes,
        iterables: list[Any],
    ) -> list[Any]:
        """Map function over iterables using Dask's native map.

        Args:
            func_bytes: cloudpickle-serialized function
            iterables: List of inputs to map over

        Returns:
            List of results
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        func = cloudpickle.loads(func_bytes)

        futures = self._client.map(func, iterables)
        return self._client.gather(futures)
