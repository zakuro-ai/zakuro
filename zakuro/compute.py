"""Compute resource abstraction for Zakuro."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


#: Schemes that address a framework or a worker directly, bypassing zc.
_DIRECT_SCHEMES = ("spark", "ray", "dask", "tcp", "quic", "zakuro", "http", "https")


def _is_loopback_authority(authority: str) -> bool:
    host = authority.split("@")[-1].rsplit(":", 1)[0].strip("[]")
    return host in ("127.0.0.1", "localhost", "::1", "")


def _looks_like_ip(authority: str) -> bool:
    import ipaddress

    host = authority.split("@")[-1].rsplit(":", 1)[0].strip("[]")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def reject_direct_addressing(uri: str) -> None:
    """Refuse a URI that reaches past zc.

    Work must enter the mesh through a broker: that is where it is authorised,
    routed, billed and attributed to the node that ran it. A `spark://` or
    `ray://` URI dials a cluster port from the client, so the job is unbilled,
    the node that did the work is unpaid, and the cluster has to publish a port
    to the world for it to be possible at all. The clusters bind loopback and
    only brokers transit the mesh, so those URIs cannot work off-host anyway --
    this turns a confusing connection error into a statement of the rule.

    Addressing another node by raw IP is refused for the same reason and one
    more: an address is not an identity. `zc://node-<fingerprint>` names a node
    in a way that survives it moving, and is what the roster authorises.

    Loopback is allowed: talking to your OWN broker is not addressing a node,
    and it is how a container reaches the broker beside it.

    Set ``ZAKURO_ALLOW_DIRECT=1`` to bypass this -- for a single-machine dev
    loop with no broker, and nothing else.
    """
    import os

    if os.environ.get("ZAKURO_ALLOW_DIRECT") == "1":
        return
    if "://" not in uri:
        return
    scheme, authority = uri.split("://", 1)
    scheme = scheme.lower()
    authority = authority.rstrip("/")

    # Loopback is allowed for ANY scheme: a cluster or worker on 127.0.0.1 is
    # this machine's own, nothing else can reach it, and driving it from inside
    # the node is exactly how a job that arrived through zc is meant to run.
    # The rule is about reaching ACROSS the mesh, not about local transports.
    if _is_loopback_authority(authority):
        return

    if scheme in _DIRECT_SCHEMES:
        raise ValueError(
            f"{uri!r} addresses a {scheme} endpoint directly. Work has to go "
            "through a broker to be routed, billed and attributed, and the "
            "clusters are not reachable off-host in any case. Use "
            "zk.Compute(uri=node.uri) for a node from zk.nodes(), or "
            "ZAKURO_ALLOW_DIRECT=1 for a local dev loop."
        )
    if (
        scheme in ("zc", "broker")
        and _looks_like_ip(authority)
        and not _is_loopback_authority(authority)
    ):
        raise ValueError(
            f"{uri!r} addresses a node by IP. An address is not an identity: "
            "use zc://node-<fingerprint>, which survives the node moving and "
            "is what the roster authorises. zk.nodes() lists them."
        )


def _is_node_uri(uri: str) -> bool:
    """Whether `uri` names a mesh node rather than a host.

    `zc://node-<16 hex>` is a node fingerprint; `zc://host:9000` is an address.
    Matched on the shape of the fingerprint so a machine that happens to be
    *called* "node-something" is not mistaken for one.
    """
    rest = uri.split("://", 1)[-1].rstrip("/")
    if not rest.startswith("node-"):
        return False
    fp = rest[len("node-") :].split(":")[0]
    return len(fp) == 16 and all(c in "0123456789abcdef" for c in fp)


@dataclass
class Compute:
    """
    Represents compute resources for remote execution.

    Supports URI-based processor selection:
        >>> compute = Compute(uri="ray://head:10001", cpus=2)
        >>> compute = Compute(uri="dask://scheduler:8786", cpus=4)
        >>> compute = Compute(uri="spark://master:7077", memory="8Gi")
        >>> compute = Compute(uri="zc://localhost:9000")  # Local broker

    Or traditional host/port (defaults to zc:// broker scheme):
        >>> compute = Compute(host="localhost", port=9000, cpus=2)

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
    memory: str | None = None
    gpus: int = 0
    image: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    # URI-based processor selection
    uri: str | None = None

    # Legacy connection settings (used if uri not provided)
    host: str | None = None
    port: int = 9000  # Default broker port

    # Backend-specific options
    processor_options: dict[str, Any] = field(default_factory=dict)

    # Probe the backend at construction when uri is explicit. Set to False to
    # inspect a Compute without performing network I/O (e.g. in tests).
    verify: bool = True

    # Set when `uri` addressed a node (`zc://node-<fp>`): the node it resolved
    # to, kept so a caller can check where work actually landed.
    _node: Any = None

    # Optional price hint, in USD per hour (or any consistent unit — the
    # allocator only ever uses ratios). When ``AdaptiveCompute`` is built
    # with ``cost_coefficient > 0``, workers with higher price are
    # deprioritised proportionally to (price * predicted_time).
    price_per_hour: float | None = None

    # Optional topology tags. When the allocator knows its own region/rack
    # (see ``AdaptiveCompute(local_region=…, local_rack=…)``), it softly
    # prefers same-region / same-rack workers on otherwise-tied decisions.
    # Never a hard constraint — a sufficiently faster remote worker still
    # wins.
    region: str | None = None
    rack: str | None = None

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
                f"Invalid memory format: {self.memory}. Expected format like '1Gi', '512Mi', '2G'"
            )

    def _resolve_uri(self) -> None:
        """Resolve URI from explicit uri or host/port.

        If neither uri nor host is given, uri stays ``None`` — detection runs
        at call time and falls back to standalone (in-process) execution when
        no backend is reachable.
        """
        if self.uri is not None:
            reject_direct_addressing(self.uri)
            # `zc://node-<fingerprint>` names a NODE, not a host. Resolve it to
            # that node's own broker before anything tries to treat the
            # fingerprint as a DNS name (which is what used to happen, and the
            # error blamed DNS for a perfectly valid node id).
            if _is_node_uri(self.uri):
                from zakuro.mesh import NodeInfo, resolve_node

                # A NodeInfo already carries its endpoint, so the common case
                # -- looping over `nodes()` -- needs no lookup at all.
                node = self._node if isinstance(self._node, NodeInfo) else resolve_node(self.uri)
                host, _, port = node.endpoint.partition(":")
                self.host = host
                self.port = int(port or 9000)
                self._node = node
                # Rewrite the URI to the concrete endpoint as well. Everything
                # downstream (processor selection, ProcessorConfig.from_uri)
                # re-parses `uri`, and a fingerprint left in the authority is
                # read as a hostname.
                self.uri = f"zc://{host}:{self.port}"
                return

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
        # A node URI resolved by answering /health -- probing again would only
        # ask the same question a second time.
        if getattr(self, "_node", None) is not None:
            return
        if self.scheme == "quic":
            try:
                socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)
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
                raise ConnectionError(f"Worker unreachable at {self.endpoint}")
            info = client.info()
        except ConnectionError:
            raise
        except Exception as exc:
            raise ConnectionError(f"Worker unreachable at {self.endpoint}: {exc}") from exc
        finally:
            client.close()

        # Validate resources against what the worker reports
        available_cpus = info.get("cpus_available")
        if available_cpus is not None and self.cpus > available_cpus:
            raise RuntimeError(
                f"Insufficient CPUs: requested {self.cpus}, available {available_cpus}"
            )

        available_memory = info.get("memory_available")
        if available_memory is not None and self.memory_bytes() > available_memory:
            raise RuntimeError(
                f"Insufficient memory: requested {self.memory} "
                f"({self.memory_bytes()} bytes), "
                f"available {available_memory} bytes"
            )

        available_gpus = info.get("gpus_available")
        if available_gpus is not None and self.gpus > available_gpus:
            raise RuntimeError(
                f"Insufficient GPUs: requested {self.gpus}, available {available_gpus}"
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
