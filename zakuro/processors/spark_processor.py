"""Spark processor for distributed computing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import cloudpickle

from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from pyspark import SparkContext

    from zakuro.compute import Compute


class SparkProcessor(Processor):
    """Spark-based processor for distributed computing.

    Uses PySpark's SparkContext for task execution with resource mapping
    to Spark executor configuration.

    URI schemes:
        - spark://host:port

    Resource mapping:
        - cpus -> spark.executor.cores
        - memory -> spark.executor.memory
        - gpus -> spark.executor.resource.gpu.amount

    Example:
        >>> config = ProcessorConfig.from_uri("spark://master:7077")
        >>> processor = SparkProcessor(config, compute)
        >>> with processor:
        ...     result = processor.execute(func_bytes, args, kwargs)
    """

    priority: ClassVar[int] = 30
    schemes: ClassVar[tuple[str, ...]] = ("spark",)

    def __init__(self, config: ProcessorConfig, compute: Compute) -> None:
        super().__init__(config, compute)
        self._sc: SparkContext | None = None
        self._owns_context = False

    @classmethod
    def is_available(cls) -> bool:
        """Check if PySpark is installed."""
        try:
            from pyspark import SparkContext  # noqa: F401

            return True
        except ImportError:
            return False

    def connect(self) -> None:
        """Connect to Spark cluster."""
        if self._connected:
            return

        from pyspark import SparkContext

        # Check if there's an existing context
        existing = SparkContext._active_spark_context
        if existing is not None:
            self._sc = existing
            self._owns_context = False
        else:
            # Build Spark configuration
            conf = self._build_spark_conf()
            self._sc = SparkContext(conf=conf)
            self._owns_context = True

        self._connected = True

    def disconnect(self) -> None:
        """Disconnect from Spark cluster."""
        if self._sc is not None and self._owns_context:
            self._sc.stop()
        self._sc = None
        self._connected = False

    def _build_spark_conf(self) -> Any:
        """Build SparkConf from Compute resources."""
        from pyspark import SparkConf

        master = f"spark://{self._config.host}:{self._config.port}"

        conf = SparkConf()
        conf.setMaster(master)
        conf.setAppName("zakuro")

        # Map compute resources to Spark config
        if self._compute.cpus > 0:
            conf.set("spark.executor.cores", str(int(self._compute.cpus)))

        if self._compute.memory:
            # Spark accepts formats like "1g", "512m"
            memory = self._compute.memory.lower().replace("i", "")
            conf.set("spark.executor.memory", memory)

        if self._compute.gpus > 0:
            conf.set("spark.executor.resource.gpu.amount", str(self._compute.gpus))

        return conf

    def execute(self, func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Execute function on Spark cluster.

        Uses a single-element RDD to execute the function on a worker.

        Args:
            func_bytes: cloudpickle-serialized function
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            Result from Spark execution

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected or self._sc is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        def _execute_task(
            _: Any,
        ) -> Any:
            # Deserialize and execute
            func = cloudpickle.loads(func_bytes)
            return func(*args, **kwargs)

        # Create RDD with single partition and map
        rdd = self._sc.parallelize([None], 1)
        results = rdd.map(_execute_task).collect()

        return results[0]

    def submit_batch(
        self,
        func_bytes: bytes,
        batch_args: list[tuple[Any, ...]],
        batch_kwargs: list[dict[str, Any]] | None = None,
    ) -> list[Any]:
        """Submit multiple tasks in parallel using RDD.

        Args:
            func_bytes: cloudpickle-serialized function
            batch_args: List of args tuples for each invocation
            batch_kwargs: Optional list of kwargs dicts

        Returns:
            List of results in same order as inputs
        """
        if not self._connected or self._sc is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        if batch_kwargs is None:
            batch_kwargs = [{} for _ in batch_args]

        # Combine args and kwargs into tuples
        work_items = list(zip(batch_args, batch_kwargs, strict=True))

        def _execute_task(item: tuple[tuple[Any, ...], dict[str, Any]]) -> Any:
            task_args, task_kwargs = item
            func = cloudpickle.loads(func_bytes)
            return func(*task_args, **task_kwargs)

        # Create RDD and map
        rdd = self._sc.parallelize(work_items, len(work_items))
        return rdd.map(_execute_task).collect()

    def map(
        self,
        func_bytes: bytes,
        iterables: list[Any],
    ) -> list[Any]:
        """Map function over iterables using Spark's native map.

        Args:
            func_bytes: cloudpickle-serialized function
            iterables: List of inputs to map over

        Returns:
            List of results
        """
        if not self._connected or self._sc is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        def _apply_func(item: Any) -> Any:
            func = cloudpickle.loads(func_bytes)
            return func(item)

        rdd = self._sc.parallelize(iterables)
        return rdd.map(_apply_func).collect()
