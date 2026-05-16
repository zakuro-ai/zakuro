"""HTTP client for communicating with Zakuro workers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import httpx

from zakuro.observability import init_sentry

if TYPE_CHECKING:
    from zakuro.compute import Compute
    from zakuro.config import Config


# Idempotent in practice: sentry_sdk.init is safe to call repeatedly and
# returns the same hub. We trigger it lazily at first client construction
# so client-side imports of zakuro stay free of Sentry side-effects until
# something actually talks to a worker.
_SENTRY_INITIALISED = False


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
        config: Optional[Config] = None,
    ) -> None:
        global _SENTRY_INITIALISED
        if not _SENTRY_INITIALISED:
            init_sentry("client")
            _SENTRY_INITIALISED = True
        self._compute = compute
        self._config = config
        self._client: Optional[httpx.Client] = None

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
            self._client = httpx.Client(
                base_url=self._compute.endpoint,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=300.0,  # 5 min for long computations
                    write=60.0,
                    pool=10.0,
                ),
                headers=self._auth_headers(),
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        """Build authentication headers if configured."""
        headers: dict[str, str] = {}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        return headers

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
        headers = {"Content-Type": "application/octet-stream"}
        if extra_headers:
            headers.update(extra_headers)
        response = self.client.post(
            "/execute",
            content=payload,
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
        return response.json()

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
