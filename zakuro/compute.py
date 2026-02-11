"""Compute resource abstraction for Zakuro."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    pass


@dataclass
class Compute:
    """
    Represents compute resources for remote execution.

    Supports URI-based processor selection:
        >>> compute = Compute(uri="ray://head:10001", cpus=2)
        >>> compute = Compute(uri="dask://scheduler:8786", cpus=4)
        >>> compute = Compute(uri="spark://master:7077", memory="8Gi")
        >>> compute = Compute(uri="zakuro://worker:8000")  # Default HTTP

    Or traditional host/port (defaults to zakuro:// scheme):
        >>> compute = Compute(host="worker", port=8000, cpus=2)

    URI Schemes:
        - zakuro:// - HTTP backend (default)
        - ray:// - Ray distributed computing
        - dask:// or tcp:// - Dask distributed
        - spark:// - Apache Spark

    Args:
        cpus: Number of CPUs (can be fractional, e.g., 0.5)
        memory: Memory allocation (e.g., "1Gi", "512Mi")
        gpus: Number of GPUs
        image: Docker image for worker (optional)
        env: Environment variables for worker
        uri: Processor URI (e.g., "ray://head:10001")
        host: Override worker host (used if uri not provided)
        port: Worker port (used if uri not provided)
        processor_options: Additional backend-specific options
    """

    cpus: float = 1.0
    memory: str = "1Gi"
    gpus: int = 0
    image: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)

    # URI-based processor selection
    uri: Optional[str] = None

    # Legacy connection settings (used if uri not provided)
    host: Optional[str] = None
    port: int = 8000

    # Backend-specific options
    processor_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and normalize resources."""
        self._validate_memory()
        self._resolve_uri()

    def _validate_memory(self) -> None:
        """Validate memory format (e.g., '1Gi', '512Mi')."""
        pattern = r"^\d+(\.\d+)?(Gi|Mi|Ki|G|M|K)?$"
        if not re.match(pattern, self.memory):
            raise ValueError(
                f"Invalid memory format: {self.memory}. "
                "Expected format like '1Gi', '512Mi', '2G'"
            )

    def _resolve_uri(self) -> None:
        """Resolve URI from explicit uri or host/port."""
        if self.uri is not None:
            # Parse URI to extract host/port for backward compatibility
            from zakuro.processors.base import ProcessorConfig

            config = ProcessorConfig.from_uri(self.uri)
            # Update host/port from URI if not explicitly set
            if self.host is None:
                self.host = config.host
            if self.port == 8000:  # default port
                self.port = config.port
        elif self.host is None:
            # No URI and no host - discover worker
            self.host = self._discover_worker()
            self.uri = f"zakuro://{self.host}:{self.port}"
        else:
            # Build URI from host/port
            self.uri = f"zakuro://{self.host}:{self.port}"

    def _discover_worker(self) -> str:
        """Discover worker via Tailscale or fallback to localhost."""
        from zakuro.discovery import discover_worker

        return discover_worker()

    @property
    def scheme(self) -> str:
        """Get the processor scheme from URI."""
        if self.uri is None:
            return "zakuro"
        from zakuro.processors.base import ProcessorConfig

        return ProcessorConfig.from_uri(self.uri).scheme

    @property
    def endpoint(self) -> str:
        """Get the worker HTTP endpoint."""
        return f"http://{self.host}:{self.port}"

    def memory_bytes(self) -> int:
        """Convert memory string to bytes."""
        match = re.match(r"^(\d+(?:\.\d+)?)(Gi|Mi|Ki|G|M|K)?$", self.memory)
        if not match:
            raise ValueError(f"Invalid memory format: {self.memory}")

        value = float(match.group(1))
        unit = match.group(2) or ""

        multipliers = {
            "": 1,
            "K": 1024,
            "Ki": 1024,
            "M": 1024**2,
            "Mi": 1024**2,
            "G": 1024**3,
            "Gi": 1024**3,
        }
        return int(value * multipliers[unit])

    def __repr__(self) -> str:
        """Return string representation."""
        parts = [f"cpus={self.cpus}", f"memory='{self.memory}'"]
        if self.gpus:
            parts.append(f"gpus={self.gpus}")
        if self.image:
            parts.append(f"image='{self.image}'")
        return f"Compute({', '.join(parts)})"
