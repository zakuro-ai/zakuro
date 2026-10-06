"""HTTP client for communicating with Zakuro workers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import httpx

from zakuro.wire.keys import wrap_payload

if TYPE_CHECKING:
    from zakuro.compute import Compute
    from zakuro.config import Config


class ZakuroClient:
    """
    HTTP client for worker communication.

    Handles:
    - Connection pooling
    - Retry logic
    - Authentication (if configured)

    Example:
        >>> compute = Compute(host="worker.local")
        >>> client = ZakuroClient(compute)
        >>> result = client.execute(serialized_payload)
    """

    def __init__(
        self,
        compute: Compute,
        config: Config | None = None,
    ) -> None:
        self._compute = compute
        self._config = config
        self._client: httpx.Client | None = None

    @property
    def config(self) -> Config:
        """Lazy-load configuration."""
        if self._config is None:
            from zakuro.config import Config

            self._config = Config.load()
        return self._config

    @property
    def client(self) -> httpx.Client:
        """Lazy-initialize HTTP client."""
        if self._client is None:
            from zakuro.client_tls import client_ssl_context
            from zakuro.mesh import proxy_for

            # Opt-in mTLS (ZAKURO_CLIENT_CERT_DIR); None keeps httpx defaults.
            ssl_ctx = client_ssl_context()
            verify_kw: dict[str, Any] = {} if ssl_ctx is None else {"verify": ssl_ctx}
            self._client = httpx.Client(
                base_url=self._compute.endpoint,
                # A mesh address is reachable only through the sidecar's
                # CONNECT proxy; loopback must not go through it. Without this
                # a class instantiated on a mesh node timed out on connect,
                # while the same node worked fine for `@zk.fn` (which goes
                # through BrokerProcessor, where the proxy was already wired).
                proxy=proxy_for(str(self._compute.host or "")),
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=300.0,  # 5 min for long computations
                    write=60.0,
                    pool=10.0,
                ),
                headers=self._auth_headers(),
                **verify_kw,
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        """Build authentication headers if configured.

        Falls back to the credentials file `zc login` writes, so a remote
        class works from a plain shell for the same reason `@zk.fn` does --
        an anonymous caller is refused or stalled on a billing-enabled fleet.
        """
        from zakuro.mesh import api_key

        headers: dict[str, str] = {}
        token = self.config.auth_token or api_key()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _requirements(self) -> dict[str, Any]:
        """The Compute spec, in the shape the broker routes on."""
        from zakuro.processors.broker import DEFAULT_MEMORY_BYTES

        compute = self._compute
        return {
            "cpus": compute.cpus,
            "memory_bytes": (
                compute.memory_bytes() if compute.memory is not None else DEFAULT_MEMORY_BYTES
            ),
            "gpus": compute.gpus,
            "estimated_duration_secs": 1.0,
        }

    def execute(self, payload: bytes, extra_headers: dict[str, str] | None = None) -> bytes:
        """
        Execute serialized function on worker.

        Args:
            payload: cloudpickle-serialized function with args/kwargs
            extra_headers: Optional additional HTTP headers (e.g. instance affinity)

        Returns:
            cloudpickle-serialized result

        Raises:
            httpx.HTTPStatusError: If worker returns error status
            httpx.ConnectError: If worker is unreachable
        """
        headers = {
            "Content-Type": "application/octet-stream",
            # What the Compute asked for. Without this the broker routes on
            # its defaults, so `Compute(gpus=1)` was silently ignored on the
            # class path and the instance could land on a CPU-only worker.
            "X-Zakuro-Requirements": json.dumps(self._requirements()),
        }
        if extra_headers:
            headers.update(extra_headers)
        response = self.client.post(
            "/execute",
            content=wrap_payload(payload),
            headers=headers,
        )
        response.raise_for_status()
        return response.content

    def ping(self) -> bool:
        """Check worker health."""
        try:
            response = self.client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def info(self) -> dict:
        """Get worker information."""
        response = self.client.get("/info")
        response.raise_for_status()
        return cast("dict[Any, Any]", response.json())

    def close(self) -> None:
        """Close HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ZakuroClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"ZakuroClient(endpoint='{self._compute.endpoint}')"
