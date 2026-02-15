"""Broker processor for routing through zc broker."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

import cloudpickle
import httpx

from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from zakuro.compute import Compute


class BrokerProcessor(Processor):
    """Broker-based processor that routes through zc broker.

    The broker handles:
    - Worker selection based on price-per-compute
    - Credit management and billing
    - Load balancing across multiple backends
    - Backend abstraction (Ray, Dask, Spark, HTTP)

    URI schemes:
        - zc://host:port (canonical)
        - broker://host:port

    Example:
        >>> config = ProcessorConfig.from_uri("zc://localhost:9000")
        >>> processor = BrokerProcessor(config, compute)
        >>> with processor:
        ...     result = processor.execute(func_bytes, args, kwargs)
    """

    priority: ClassVar[int] = 100  # Highest priority when available
    schemes: ClassVar[tuple[str, ...]] = ("zc", "broker")

    def __init__(self, config: ProcessorConfig, compute: Compute) -> None:
        super().__init__(config, compute)
        self._client: httpx.Client | None = None
        self._user_id: str | None = None

    @classmethod
    def is_available(cls) -> bool:
        """Broker processor is always available (uses httpx)."""
        return True

    def connect(self) -> None:
        """Initialize HTTP client connection to broker."""
        if self._connected:
            return

        import os

        # Read API key from ZAKURO_AUTH env var
        self._api_key: str | None = os.environ.get("ZAKURO_AUTH")

        # Determine user_id:
        # 1. processor_options["user_id"] if set
        # 2. Extract from API key format (zk_{user_id}_{random})
        # 3. ZAKURO_USER env var
        # 4. $USER / "anonymous"
        user_id = self._compute.processor_options.get("user_id") if self._compute.processor_options else None
        if not user_id and self._api_key and self._api_key.startswith("zk_"):
            # Extract user_id from key format: zk_{user_id}_{random}
            parts = self._api_key[3:]  # strip "zk_"
            last_underscore = parts.rfind("_")
            if last_underscore > 0:
                user_id = parts[:last_underscore]
        if not user_id:
            user_id = os.environ.get("ZAKURO_USER", os.environ.get("USER", "anonymous"))
        self._user_id = user_id

        # Build broker URL
        broker_url = f"http://{self._config.host}:{self._config.port}"

        # Set default headers: prefer Bearer auth, fallback to X-Zakuro-User
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["X-Zakuro-User"] = self._user_id

        self._client = httpx.Client(
            base_url=broker_url,
            headers=headers,
            timeout=httpx.Timeout(
                connect=10.0,
                read=300.0,  # 5 min for long computations
                write=60.0,
                pool=10.0,
            ),
        )
        self._connected = True

    def disconnect(self) -> None:
        """Close HTTP client connection."""
        if self._client is not None:
            self._client.close()
            self._client = None
        self._connected = False

    def execute(self, func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Execute function via broker.

        The broker will:
        1. Check user credits
        2. Select optimal worker based on price/resources
        3. Forward request to worker
        4. Charge credits based on actual usage
        5. Return result

        Args:
            func_bytes: cloudpickle-serialized function
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            Deserialized result from worker

        Raises:
            RuntimeError: If not connected or insufficient credits
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        # Deserialize function to repackage with args
        func = cloudpickle.loads(func_bytes)

        # Serialize function and arguments together
        payload = cloudpickle.dumps(
            {
                "func": func,
                "args": args,
                "kwargs": kwargs,
            }
        )

        # Build requirements header
        requirements = {
            "cpus": self._compute.cpus,
            "memory_bytes": self._compute.memory_bytes(),
            "gpus": self._compute.gpus,
            "estimated_duration_secs": 1.0,  # Default estimate
        }

        # Execute via broker (auth headers are set on the client)
        response = self._client.post(
            "/execute",
            content=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Zakuro-Requirements": json.dumps(requirements),
            },
        )

        # Check for errors
        if response.status_code == 402:
            raise RuntimeError("Insufficient credits for compute request")
        if response.status_code == 429:
            raise RuntimeError("Rate limit exceeded")
        if response.status_code == 503:
            raise RuntimeError("No workers available")

        response.raise_for_status()

        # Extract cost info from headers
        cost = response.headers.get("X-Zakuro-Cost", "0")
        remaining = response.headers.get("X-Zakuro-Credits-Remaining", "0")

        # Deserialize result
        result = cloudpickle.loads(response.content)

        if isinstance(result, Exception):
            raise result

        return result

    def get_credits(self) -> dict[str, Any]:
        """Get current credit balance.

        Returns:
            Dict with user_id, balance, total_spent, rate_limit
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.get(f"/credits/{self._user_id}")
        response.raise_for_status()
        return response.json()

    def add_credits(self, amount: float, description: str = "API deposit") -> dict[str, Any]:
        """Add credits to account.

        Args:
            amount: Credits to add
            description: Transaction description

        Returns:
            Transaction result
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.post(
            f"/credits/{self._user_id}/add",
            json={"amount": amount, "description": description},
        )
        response.raise_for_status()
        return response.json()

    def list_workers(self) -> list[dict[str, Any]]:
        """List available workers.

        Returns:
            List of worker info dicts
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.get("/workers")
        response.raise_for_status()
        return response.json().get("workers", [])

    def estimate_price(
        self,
        cpus: float = 1.0,
        memory_gib: float = 1.0,
        gpus: int = 0,
        duration_secs: float = 1.0,
    ) -> dict[str, Any]:
        """Estimate price for a compute request.

        Args:
            cpus: Number of CPUs
            memory_gib: Memory in GiB
            gpus: Number of GPUs
            duration_secs: Estimated duration

        Returns:
            Dict with min_cost, max_cost, matching_workers
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.post(
            "/price",
            json={
                "cpus": cpus,
                "memory_bytes": int(memory_gib * 1024 * 1024 * 1024),
                "gpus": gpus,
                "estimated_duration_secs": duration_secs,
            },
        )
        response.raise_for_status()
        return response.json()

    def whoami(self) -> dict[str, Any]:
        """Get authenticated user info from broker.

        Returns:
            Dict with user_id, balance, ledger_connected, local_mode
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.get("/me")
        response.raise_for_status()
        return response.json()

    def ping(self) -> bool:
        """Check broker health."""
        if self._client is None:
            return False
        try:
            response = self._client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False
