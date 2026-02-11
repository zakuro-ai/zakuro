"""Abstract base class for compute processors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import ParseResult, urlparse

if TYPE_CHECKING:
    from zakuro.compute import Compute


@dataclass
class ProcessorConfig:
    """Configuration parsed from a processor URI.

    Example:
        >>> config = ProcessorConfig.from_uri("ray://head-node:10001")
        >>> config.scheme
        'ray'
        >>> config.host
        'head-node'
        >>> config.port
        10001
    """

    scheme: str
    host: str
    port: int
    path: str = ""
    params: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_uri(cls, uri: str) -> ProcessorConfig:
        """Parse a processor URI into configuration.

        Supported formats:
            - ray://host:port
            - dask://scheduler:8786
            - spark://master:7077
            - zakuro://worker:8000
            - tcp://scheduler:8786 (alias for dask)
            - http://host:port (alias for zakuro)
        """
        parsed: ParseResult = urlparse(uri)

        scheme = parsed.scheme.lower()
        host = parsed.hostname or "localhost"
        port = parsed.port or cls._default_port(scheme)
        path = parsed.path or ""

        # Parse query params
        params: dict[str, str] = {}
        if parsed.query:
            for param in parsed.query.split("&"):
                if "=" in param:
                    key, value = param.split("=", 1)
                    params[key] = value

        return cls(scheme=scheme, host=host, port=port, path=path, params=params)

    @staticmethod
    def _default_port(scheme: str) -> int:
        """Get default port for a scheme."""
        defaults = {
            "zakuro": 8000,
            "http": 8000,
            "https": 8000,
            "ray": 10001,
            "dask": 8786,
            "tcp": 8786,
            "spark": 7077,
            "zc": 9000,
            "broker": 9000,
        }
        return defaults.get(scheme, 8000)

    @property
    def endpoint(self) -> str:
        """Get the full endpoint URL."""
        return f"{self.scheme}://{self.host}:{self.port}{self.path}"


class Processor(ABC):
    """Abstract base class for compute processors.

    Processors handle execution of serialized functions on different backends:
    - HttpProcessor: Default HTTP-based execution via ZakuroClient
    - RayProcessor: Ray distributed computing
    - DaskProcessor: Dask distributed computing
    - SparkProcessor: Apache Spark

    Example:
        >>> processor = RayProcessor(config, compute)
        >>> with processor:
        ...     result = processor.execute(func_bytes, args, kwargs)
    """

    # Processor priority for auto-selection (higher = preferred)
    priority: ClassVar[int] = 0

    # URI schemes this processor handles
    schemes: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: ProcessorConfig, compute: Compute) -> None:
        """Initialize processor with configuration.

        Args:
            config: Parsed URI configuration
            compute: Compute resource specification
        """
        self._config = config
        self._compute = compute
        self._connected = False

    @property
    def config(self) -> ProcessorConfig:
        """Get processor configuration."""
        return self._config

    @property
    def compute(self) -> Compute:
        """Get compute resources."""
        return self._compute

    @property
    def is_connected(self) -> bool:
        """Check if processor is connected."""
        return self._connected

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Check if this processor's dependencies are installed.

        Returns:
            True if the processor can be used, False otherwise.
        """
        ...

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the compute backend.

        Called automatically when entering context manager.
        """
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to the compute backend.

        Called automatically when exiting context manager.
        """
        ...

    @abstractmethod
    def execute(self, func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Execute a serialized function on the backend.

        Args:
            func_bytes: cloudpickle-serialized function
            args: Positional arguments for the function
            kwargs: Keyword arguments for the function

        Returns:
            The function's return value (deserialized)

        Raises:
            RuntimeError: If not connected
            Exception: Any exception from the remote execution
        """
        ...

    def __enter__(self) -> Processor:
        """Enter context manager and connect."""
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        """Exit context manager and disconnect."""
        self.disconnect()

    def __repr__(self) -> str:
        """Return string representation."""
        return f"{self.__class__.__name__}(uri='{self._config.endpoint}')"
