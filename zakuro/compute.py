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
        >>> compute = Compute(uri="zc://my.zakuro-ai.com:9000")  # Production broker

    Or traditional host/port (defaults to zc:// broker scheme):
        >>> compute = Compute(host="my.zakuro-ai.com", port=9000, cpus=2)

    URI Schemes:
        - zc:// - Broker-based routing (default, recommended)
        - zakuro:// - Direct HTTP worker backend
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
    memory: Optional[str] = None
    gpus: int = 0
    image: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)

    # URI-based processor selection
    uri: Optional[str] = None

    # Legacy connection settings (used if uri not provided)
    host: Optional[str] = None
    port: int = 9000  # Default broker port

    # Backend-specific options
    processor_options: dict[str, Any] = field(default_factory=dict)

    # Probe the backend at construction when uri is explicit. Set to False to
    # inspect a Compute without performing network I/O (e.g. in tests).
    verify: bool = True

    def __post_init__(self) -> None:
        """Validate, resolve, and verify the backend is reachable when explicit."""
        uri_explicit = self.uri is not None
        self._validate_memory()
        self._resolve_uri()
        if uri_explicit and self.verify:
            self._verify_reachable()

    def _validate_memory(self) -> None:
        """Validate memory format (e.g., '1Gi', '512Mi'). None means no cap requested."""
        if self.memory is None:
            return
        pattern = r"^\d+(\.\d+)?(Gi|Mi|Ki|G|M|K)?$"
        if not re.match(pattern, self.memory):
            raise ValueError(
                f"Invalid memory format: {self.memory}. "
                "Expected format like '1Gi', '512Mi', '2G'"
            )

    def _resolve_uri(self) -> None:
        """Resolve URI from explicit uri or host/port.

        If neither uri nor host is given, uri stays ``None`` — detection runs
        at call time and falls back to standalone (in-process) execution when
        no backend is reachable.
        """
        if self.uri is not None:
            # Parse URI to extract host/port for backward compatibility
            from zakuro.processors.base import ProcessorConfig

            config = ProcessorConfig.from_uri(self.uri)
            # Update host/port from URI if not explicitly set
            if self.host is None:
                self.host = config.host
            if self.port == 9000:  # default port
                self.port = config.port
        elif self.host is not None:
            # Build URI from host/port - use broker by default
            self.uri = f"zc://{self.host}:{self.port}"

    def _verify_reachable(self) -> None:
        """Probe host:port; raise ConnectionError on failure.

        TCP connect for stream schemes; DNS resolution for QUIC (UDP-based,
        no cheap reachability probe short of a real handshake).
        """
        import socket

        if self.host is None or self.port is None:
            return
        if self.scheme == "quic":
            try:
                socket.getaddrinfo(
                    self.host, self.port, type=socket.SOCK_DGRAM
                )
                return
            except socket.gaierror as exc:
                raise ConnectionError(
                    f"Backend at {self.uri} has an unresolvable host ({exc}). "
                    "Fix the URI, or pass verify=False to skip this check."
                ) from exc
        try:
            with socket.create_connection((self.host, self.port), timeout=2.0):
                return
        except (OSError, socket.gaierror) as exc:
            raise ConnectionError(
                f"Backend at {self.uri} is not reachable ({exc}). "
                "Start the worker / cluster, fix the URI, or pass verify=False "
                "to skip this check."
            ) from exc

    def _discover_worker(self) -> str:
        """Discover worker via Tailscale or fallback to localhost."""
        from zakuro.discovery import discover_worker

        return discover_worker()

    @property
    def scheme(self) -> str:
        """Get the processor scheme from URI, or 'zc' when unresolved (standalone-capable)."""
        if self.uri is None:
            return "zc"
        from zakuro.processors.base import ProcessorConfig

        return ProcessorConfig.from_uri(self.uri).scheme

    @property
    def endpoint(self) -> str:
        """Get the worker HTTP endpoint, or a standalone marker if unresolved.

        Compute may be constructed without a URI or host — detection runs at
        call time and, if no backend is reachable, the function executes
        in-process. Accessing .endpoint before resolution emits a warning and
        returns ``"zc://in-process"`` instead of a bogus
        ``http://None:9000``.
        """
        if self.host is None:
            import warnings

            warnings.warn(
                "Compute has no resolved backend; endpoint is not meaningful in "
                "standalone mode. Call .to(...) with a uri/host or run "
                "zakuro.detect_backend() to resolve.",
                stacklevel=2,
            )
            return "zc://in-process"
        return f"http://{self.host}:{self.port}"

    def memory_bytes(self) -> int:
        """Convert memory string to bytes. Raises if no memory is set."""
        if self.memory is None:
            raise ValueError("Compute.memory is not set; cannot convert to bytes.")
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

    def check(self) -> dict:
        """
        Validate connectivity and resource availability on the target worker.

        Returns:
            Worker info dict on success.

        Raises:
            ConnectionError: If the worker is unreachable.
            RuntimeError: If the worker has insufficient resources.
        """
        from zakuro.client import ZakuroClient

        client = ZakuroClient(self)
        try:
            if not client.ping():
                raise ConnectionError(
                    f"Worker unreachable at {self.endpoint}"
                )
            info = client.info()
        except ConnectionError:
            raise
        except Exception as exc:
            raise ConnectionError(
                f"Worker unreachable at {self.endpoint}: {exc}"
            ) from exc
        finally:
            client.close()

        # Validate resources against what the worker reports
        available_cpus = info.get("cpus_available")
        if available_cpus is not None and self.cpus > available_cpus:
            raise RuntimeError(
                f"Insufficient CPUs: requested {self.cpus}, "
                f"available {available_cpus}"
            )

        available_memory = info.get("memory_available")
        if available_memory is not None:
            if self.memory_bytes() > available_memory:
                raise RuntimeError(
                    f"Insufficient memory: requested {self.memory} "
                    f"({self.memory_bytes()} bytes), "
                    f"available {available_memory} bytes"
                )

        available_gpus = info.get("gpus_available")
        if available_gpus is not None and self.gpus > available_gpus:
            raise RuntimeError(
                f"Insufficient GPUs: requested {self.gpus}, "
                f"available {available_gpus}"
            )

        return info

    def __repr__(self) -> str:
        """Return string representation."""
        parts = [f"cpus={self.cpus}", f"memory='{self.memory}'"]
        if self.gpus:
            parts.append(f"gpus={self.gpus}")
        if self.image:
            parts.append(f"image='{self.image}'")
        return f"Compute({', '.join(parts)})"
