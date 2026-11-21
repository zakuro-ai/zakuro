"""Broker processor for routing through zc broker."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar, cast

import httpx

from zakuro.errors import load_reply, mark_remote
from zakuro.processors.base import Processor, ProcessorConfig

if TYPE_CHECKING:
    from zakuro.compute import Compute


#: What to ask for when a `Compute` names no memory, which is the default.
#: `Compute.memory_bytes()` raises on None by design -- it will not invent a
#: number for a caller who asked for a specific one -- but a request with no
#: memory constraint is the normal case and must not be an error. 100 MiB is
#: what zc's own bench sends for a routing-overhead job.
DEFAULT_MEMORY_BYTES = 100 * 1024 * 1024

#: Fallbacks when the broker's reply carries no `error` of its own.
_STATUS_FALLBACK = {
    402: "Insufficient balance for compute request",
    429: "Rate limit exceeded",
    503: "No workers available",
}

#: Statuses the broker uses to say something a caller can act on.
_BROKER_ERROR_STATUSES = frozenset({400, *_STATUS_FALLBACK})

_HINTS = {
    "BUDGET_REQUIRED": (
        'pass a budget in yen: zk.Compute(uri=..., processor_options={"budget": 10})'
    ),
}


class BrokerError(RuntimeError):
    """The broker refused a request and said why.

    Still a `RuntimeError`, so existing `except RuntimeError` keeps working.
    `message` and `code` are the broker's own `error` / `code` JSON fields
    (`code` is None when it sent none); `status` is the HTTP status.
    """

    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        text = message
        hint = _HINTS.get(code or "")
        if hint:
            text = f"{text} ({hint})"
        super().__init__(text)


def _raise_for_broker_error(response: httpx.Response, fallback: str | None = None) -> None:
    """Raise `BrokerError` for a non-2xx reply, carrying the broker's own message."""
    status = response.status_code
    if status < 400:
        return
    if status not in _BROKER_ERROR_STATUSES:
        # 401/403/404/5xx keep raising httpx.HTTPStatusError, as callers expect.
        response.raise_for_status()
        return
    message: str | None = None
    code: str | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        if isinstance(body.get("error"), str) and body["error"].strip():
            message = body["error"].strip()
        if isinstance(body.get("code"), str):
            code = body["code"]
    if message is None:
        message = fallback or _STATUS_FALLBACK.get(status)
    if message is None and status == 400:
        message = "Broker rejected the request (400 Bad Request)"
    if message is None:
        response.raise_for_status()
        return
    raise BrokerError(status, message, code)


class BrokerProcessor(Processor):
    """Broker-based processor that routes through zc broker.

    The broker handles:
    - Worker selection based on price-per-compute
    - Balance management and billing (amounts in yen)
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
        self.last_cost: float = 0.0
        self.last_balance: float = 0.0

    @classmethod
    def is_available(cls) -> bool:
        """Broker processor is always available (uses httpx)."""
        return True

    def connect(self) -> None:
        """Initialize HTTP client connection to broker."""
        if self._connected:
            return

        import os

        # The account key: environment first, then the credentials file
        # `zc login` wrote. Reading the file is what lets `import zakuro` work
        # in a plain shell -- without it the broker sees an anonymous caller,
        # and on a billing-enabled fleet every request to a priced worker is
        # refused or stalls, which reads as a dead mesh.
        from zakuro.mesh import api_key as _load_api_key

        self._api_key: str | None = _load_api_key()

        # Determine user_id:
        # 1. processor_options["user_id"] if set
        # 2. Extract from API key format (zk_{user_id}_{random})
        # 3. ZAKURO_USER env var
        # 4. $USER / "anonymous"
        user_id = (
            self._compute.processor_options.get("user_id")
            if self._compute.processor_options
            else None
        )
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
            from zakuro.security import ensure_secure_for_key

            ensure_secure_for_key(broker_url)
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["X-Zakuro-User"] = self._user_id

        # A mesh address is reachable only through the sidecar's CONNECT
        # proxy; loopback must NOT go through it, or talking to your own
        # broker means leaving the host and coming back.
        from zakuro.mesh import proxy_for
        from zakuro.processors._http_pool import shared_client

        # Kept and reused across calls: a client per call meant a new TCP
        # connection to the broker for every call.
        self._client = shared_client(
            broker_url,
            headers,
            proxy_for(str(self._config.host)),
            httpx.Timeout(
                connect=10.0,
                read=300.0,  # 5 min for long computations
                write=60.0,
                pool=10.0,
            ),
        )
        self._connected = True

    def disconnect(self) -> None:
        """Release the client. It stays open for the next call on this thread."""
        self._client = None
        self._connected = False

    def _requirements(self) -> dict[str, Any]:
        """The Compute spec, in the shape the broker routes on.

        Shared by the stateless `execute` path and the stateful `@cls` one, so
        an instance is placed against the same spec its caller asked for --
        `Compute(gpus=1)` must mean the same thing for a class as a function.
        """
        opts = self._compute.processor_options or {}
        requirements: dict[str, Any] = {
            "cpus": self._compute.cpus,
            "memory_bytes": (
                self._compute.memory_bytes()
                if self._compute.memory is not None
                else DEFAULT_MEMORY_BYTES
            ),
            "gpus": self._compute.gpus,
            "estimated_duration_secs": opts.get("estimated_duration_secs", 1.0),
        }
        if "budget" in opts:
            requirements["budget"] = float(opts["budget"])
        if "strategy" in opts:
            requirements["strategy"] = opts["strategy"]
        if "remote_only" in opts:
            requirements["remote_only"] = bool(opts["remote_only"])
        if "worker_type" in opts:
            requirements["worker_type"] = opts["worker_type"]
        # Pin execution to a specific mesh worker/node by zc:// address (or bare
        # name). The broker normalizes the scheme and routes the offer to the
        # owning node, rejecting it elsewhere. Absent = normal strategy routing.
        if opts.get("target_worker"):
            requirements["target_worker"] = str(opts["target_worker"])
        if opts.get("target_node"):
            requirements["target_node"] = str(opts["target_node"])
        return requirements

    def execute_payload(self, payload: bytes, extra_headers: dict[str, str] | None = None) -> bytes:
        """Send a pre-serialized request through the broker, for `@cls`.

        The broker already speaks instance affinity: it reads
        `X-Zakuro-Instance-Action` / `X-Zakuro-Instance-Id`, pins the
        instance id to the worker that created it, and routes later
        `call_method` calls back to that same worker. Only the SDK side was
        missing, so `@zk.cls` over a broker raised NotImplementedError while
        the same class worked over a direct HTTP worker.

        Returns raw cloudpickle bytes -- a result or an exception -- which the
        proxy deserialises and raises.
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        headers = {
            "Content-Type": "application/octet-stream",
            "X-Zakuro-Requirements": json.dumps(self._requirements()),
        }
        if extra_headers:
            headers.update(extra_headers)

        response = self._client.post("/execute", content=payload, headers=headers)
        # Same mapping as `execute`: the broker's own message when it sent one.
        _raise_for_broker_error(
            response,
            fallback=(
                "No worker matched this Compute (check gpus/cpus/memory against "
                "`zakuro.nodes()`), or the node refused the request"
            )
            if response.status_code == 503
            else None,
        )
        return bytes(response.content)

    def execute(self, func_bytes: bytes, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Execute function via broker.

        The broker will:
        1. Check user balance
        2. Select optimal worker based on price/resources
        3. Forward request to worker
        4. Charge the balance (yen) based on actual usage
        5. Return result

        Args:
            func_bytes: cloudpickle-serialized function
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            Deserialized result from worker

        Raises:
            RuntimeError: If not connected or insufficient balance
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected. Use as context manager.")

        # The function travels as the bytes the caller pickled: no second
        # unpickle and re-pickle here, and identical bytes for an unchanged
        # function, which a worker can cache by hash.
        from zakuro.processors._payload import build_execute_payload

        payload = build_execute_payload(func_bytes, args, kwargs)

        # Build requirements header from compute spec + processor_options overrides
        requirements = self._requirements()

        # Execute via broker (auth headers are set on the client)
        response = self._client.post(
            "/execute",
            content=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Zakuro-Requirements": json.dumps(requirements),
            },
        )

        # Check for errors (the broker's own message wins over a generic one)
        _raise_for_broker_error(response)

        # Extract cost info from headers
        cost = response.headers.get("X-Zakuro-Cost", "0")
        remaining = response.headers.get("X-Zakuro-Balance", "0")
        try:
            self.last_cost = float(cost)
        except (ValueError, TypeError):
            self.last_cost = 0.0
        try:
            self.last_balance = float(remaining)
        except (ValueError, TypeError):
            self.last_balance = 0.0

        # Deserialize result
        result = load_reply(response.content)

        if isinstance(result, Exception):
            # Raised by the remote code, not by reaching it: never retried.
            raise mark_remote(result)

        return result

    def get_balance(self) -> dict[str, Any]:
        """Get current balance (yen).

        Returns:
            Dict with user_id, balance, total_spent, rate_limit
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.get(f"/balance/{self._user_id}")
        _raise_for_broker_error(response)
        return cast("dict[str, Any]", response.json())

    def add_balance(self, amount: float, description: str = "API deposit") -> dict[str, Any]:
        """Add money (yen) to the account balance.

        Args:
            amount: Amount to add, in yen
            description: Transaction description

        Returns:
            Transaction result
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.post(
            f"/balance/{self._user_id}/add",
            json={"amount": amount, "description": description},
        )
        _raise_for_broker_error(response)
        return cast("dict[str, Any]", response.json())

    def list_workers(self) -> list[dict[str, Any]]:
        """List available workers.

        Returns:
            List of worker info dicts
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.get("/workers")
        _raise_for_broker_error(response)
        return cast("list[dict[str, Any]]", response.json().get("workers", []))

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
        _raise_for_broker_error(response)
        return cast("dict[str, Any]", response.json())

    def whoami(self) -> dict[str, Any]:
        """Get authenticated user info from broker.

        Returns:
            Dict with user_id, balance, ledger_connected, local_mode
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Processor not connected.")

        response = self._client.get("/me")
        _raise_for_broker_error(response)
        return cast("dict[str, Any]", response.json())

    def ping(self) -> bool:
        """Check broker health."""
        if self._client is None:
            return False
        try:
            response = self._client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False
