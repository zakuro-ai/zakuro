"""Processor registry for auto-discovery and selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from zakuro.compute import Compute


class ProcessorRegistry:
    """Registry for processor backends.

    Handles auto-discovery of installed processors and selection based on URI scheme.
    Uses singleton pattern to cache discovery results.

    Example:
        >>> registry = ProcessorRegistry()
        >>> processor = registry.get("ray://head:10001", compute)
        >>> print(registry.available())
        ['zakuro', 'ray', 'dask', 'spark']
    """

    _instance: ProcessorRegistry | None = None
    _processors: dict[str, type[Processor]] | None = None

    def __new__(cls) -> ProcessorRegistry:
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def processors(self) -> dict[str, type[Processor]]:
        """Lazy-load and cache available processors."""
        if self._processors is None:
            self._processors = self._discover_processors()
        return self._processors

    def _discover_processors(self) -> dict[str, type[Processor]]:
        """Discover all available processors.

        Returns a dict mapping scheme names to processor classes.
        """
        discovered: dict[str, type[Processor]] = {}

        # Always import HTTP processor (no external deps)
        from zakuro.processors.http import HttpProcessor

        for scheme in HttpProcessor.schemes:
            discovered[scheme] = HttpProcessor

        # Always import Broker processor (no external deps)
        from zakuro.processors.broker import BrokerProcessor

        for scheme in BrokerProcessor.schemes:
            discovered[scheme] = BrokerProcessor

        # Try importing optional processors
        try:
            from zakuro.processors.ray_processor import RayProcessor

            if RayProcessor.is_available():
                for scheme in RayProcessor.schemes:
                    discovered[scheme] = RayProcessor
        except ImportError:
            pass

        try:
            from zakuro.processors.dask_processor import DaskProcessor

            if DaskProcessor.is_available():
                for scheme in DaskProcessor.schemes:
                    discovered[scheme] = DaskProcessor
        except ImportError:
            pass

        try:
            from zakuro.processors.spark_processor import SparkProcessor

            if SparkProcessor.is_available():
                for scheme in SparkProcessor.schemes:
                    discovered[scheme] = SparkProcessor
        except ImportError:
            pass

        return discovered

    def get(self, uri: str, compute: Compute) -> Processor:
        """Get a processor instance for the given URI.

        Args:
            uri: Processor URI (e.g., "ray://head:10001")
            compute: Compute resource specification

        Returns:
            Configured processor instance

        Raises:
            ValueError: If scheme is not supported or processor unavailable
        """
        config = ProcessorConfig.from_uri(uri)

        if config.scheme not in self.processors:
            available = ", ".join(sorted(self.available()))
            raise ValueError(
                f"Unsupported processor scheme: '{config.scheme}'. "
                f"Available: {available}"
            )

        processor_cls = self.processors[config.scheme]
        return processor_cls(config, compute)

    def get_best(self, compute: Compute) -> Processor:
        """Get the best available processor based on priority.

        Uses priority order: Ray (50) > Dask (40) > Spark (30) > HTTP (10)

        Args:
            compute: Compute resource specification

        Returns:
            Processor instance with highest priority
        """
        # Get unique processor classes with their priorities
        processor_classes: dict[type[Processor], int] = {}
        for proc_cls in self.processors.values():
            if proc_cls not in processor_classes:
                processor_classes[proc_cls] = proc_cls.priority

        # Sort by priority descending
        sorted_procs = sorted(processor_classes.items(), key=lambda x: x[1], reverse=True)

        if not sorted_procs:
            raise RuntimeError("No processors available")

        best_cls = sorted_procs[0][0]

        # Use default URI for the best processor
        default_scheme = best_cls.schemes[0]
        default_port = ProcessorConfig._default_port(default_scheme)
        host = compute.host or "localhost"
        default_uri = f"{default_scheme}://{host}:{default_port}"

        return self.get(default_uri, compute)

    def available(self) -> list[str]:
        """List available processor schemes.

        Returns:
            List of scheme names that can be used in URIs
        """
        return sorted(set(self.processors.keys()))

    def available_processors(self) -> list[str]:
        """List available processor names (canonical schemes only).

        Returns:
            List of canonical processor names (zakuro, ray, dask, spark)
        """
        # Map schemes to canonical names
        canonical = set()
        for proc_cls in self.processors.values():
            canonical.add(proc_cls.schemes[0])
        return sorted(canonical)

    def refresh(self) -> None:
        """Force re-discovery of processors."""
        self._processors = None


# Module-level convenience functions
_registry: ProcessorRegistry | None = None


def _get_registry() -> ProcessorRegistry:
    """Get the singleton registry instance."""
    global _registry
    if _registry is None:
        _registry = ProcessorRegistry()
    return _registry


def get_processor(uri: str, compute: Compute) -> Processor:
    """Get a processor for the given URI.

    Args:
        uri: Processor URI (e.g., "ray://head:10001", "zakuro://worker:8000")
        compute: Compute resource specification

    Returns:
        Configured processor instance

    Example:
        >>> processor = get_processor("ray://head:10001", compute)
        >>> with processor:
        ...     result = processor.execute(func_bytes, args, kwargs)
    """
    return _get_registry().get(uri, compute)


def get_best_processor(compute: Compute) -> Processor:
    """Get the best available processor based on priority.

    Args:
        compute: Compute resource specification

    Returns:
        Processor with highest priority

    Example:
        >>> processor = get_best_processor(compute)
        >>> print(processor)  # RayProcessor if ray installed
    """
    return _get_registry().get_best(compute)


def available_processors() -> list[str]:
    """List available processor names.

    Returns:
        List of available processor names (e.g., ['zakuro', 'ray'])

    Example:
        >>> import zakuro as zk
        >>> print(zk.available_processors())
        ['dask', 'ray', 'zakuro']
    """
    return _get_registry().available_processors()
