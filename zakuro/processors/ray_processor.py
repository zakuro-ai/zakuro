"""Ray processor for distributed computing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import cloudpickle

from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from zakuro.compute import Compute


class RayProcessor(Processor):
    """Ray-based processor for distributed computing.

    Uses Ray's @ray.remote decorator for task execution with automatic
    resource allocation based on Compute specification.

    URI schemes:
        - ray://host:port

    Resource mapping:
        - cpus -> num_cpus
        - memory -> memory (bytes)
        - gpus -> num_gpus

    Example:
        >>> config = ProcessorConfig.from_uri("ray://head:10001")
        >>> processor = RayProcessor(config, compute)
        >>> with processor:
        ...     result = processor.execute(func_bytes, args, kwargs)
    """

    priority: ClassVar[int] = 50
    schemes: ClassVar[tuple[str, ...]] = ("ray",)

    def __init__(self, config: ProcessorConfig, compute: Compute) -> None:
        super().__init__(config, compute)
        self._ray: Any = None

    @classmethod
    def is_available(cls) -> bool:
        """Check if Ray is installed."""
        try:
            import ray  # noqa: F401

            return True
        except ImportError:
            return False

    def connect(self) -> None:
        """Connect to Ray cluster."""
        if self._connected:
            return

        import ray

        self._ray = ray

        # Build connection address
        address = f"ray://{self._config.host}:{self._config.port}"

        # Check if already connected
        if not ray.is_initialized():
            ray.init(address=address, ignore_reinit_error=True)

        self._connected = True

    def disconnect(self) -> None:
        """Disconnect from Ray cluster.

        Note: We don't call ray.shutdown() as it would affect other users.
        """
        self._connected = False

    def execute(self, func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Execute function on Ray cluster.

        Args:
            func_bytes: cloudpickle-serialized function
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            Result from Ray execution

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected or self._ray is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        ray = self._ray

        # Build resource options
        options = self._build_ray_options()

        # Create remote function with resources
        @ray.remote(**options)
        def _execute_task(
            serialized_func: bytes, task_args: tuple[Any, ...], task_kwargs: dict[str, Any]
        ) -> Any:
            func = cloudpickle.loads(serialized_func)
            return func(*task_args, **task_kwargs)

        # Submit and wait for result
        future = _execute_task.remote(func_bytes, args, kwargs)
        return ray.get(future)

    def _build_ray_options(self) -> dict[str, Any]:
        """Build Ray remote options from Compute resources."""
        options: dict[str, Any] = {}

        if self._compute.cpus > 0:
            options["num_cpus"] = self._compute.cpus

        if self._compute.gpus > 0:
            options["num_gpus"] = self._compute.gpus

        # Convert memory string to bytes for Ray
        if self._compute.memory:
            options["memory"] = self._compute.memory_bytes()

        return options

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
        if not self._connected or self._ray is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        ray = self._ray

        if batch_kwargs is None:
            batch_kwargs = [{} for _ in batch_args]

        options = self._build_ray_options()

        @ray.remote(**options)
        def _execute_task(
            serialized_func: bytes, task_args: tuple[Any, ...], task_kwargs: dict[str, Any]
        ) -> Any:
            func = cloudpickle.loads(serialized_func)
            return func(*task_args, **task_kwargs)

        # Submit all tasks
        futures = [
            _execute_task.remote(func_bytes, args, kwargs)
            for args, kwargs in zip(batch_args, batch_kwargs, strict=True)
        ]

        # Wait for all results
        return ray.get(futures)
