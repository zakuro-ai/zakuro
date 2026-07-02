"""Prometheus metrics for Zakuro (closes #124 — RFC 0003 Track B).

Design notes:

* The single entry point is :func:`init_metrics`. It enables instrumentation
  by toggling a module-level flag — the underlying collectors are imported
  unconditionally so the metric names exist even if ``prometheus_client`` is
  not installed (in that case the collectors are stubbed and ``init_metrics``
  returns ``False``).
* Naming follows
  `Prometheus best practices <https://prometheus.io/docs/practices/naming/>`_:
  ``<namespace>_<subsystem>_<name>_<unit>``.
* Label cardinality is **strictly bounded**. We refuse to add labels that
  could blow up the TSDB:
    - ``tenant_id``  — bounded (~hundreds);
    - ``worker_id``  — bounded (~tens);
    - ``path`` / ``method`` — enumerated by FastAPI routes.
* :func:`mount_metrics_endpoint` registers ``/metrics`` on a FastAPI app
  with appropriate ``text/plain; version=0.0.4`` content-type. Auth is
  the caller's responsibility — once RFC 0002 lands, gate it on the
  ``metrics:read`` JWT scope.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

try:  # prometheus_client is an optional dependency
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _ENABLED = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _ENABLED = False

    # Stub the names we re-export so type checkers / callers don't break.
    # The collectors are instantiated unconditionally at import time (e.g.
    # ``Counter(name, doc, labelnames=...)`` below), so the stub MUST accept the
    # same constructor arguments — otherwise importing this module without
    # ``prometheus_client`` raises ``TypeError: _NoopMetric() takes no arguments``
    # and every worker that imports metrics (the default HTTP/FastAPI server)
    # crashes on startup.
    class _NoopMetric:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

        def labels(self, *_args: Any, **_kwargs: Any) -> _NoopMetric:
            return self

        def inc(self, *_args: Any, **_kwargs: Any) -> None: ...

        def dec(self, *_args: Any, **_kwargs: Any) -> None: ...

        def observe(self, *_args: Any, **_kwargs: Any) -> None: ...

        def set(self, *_args: Any, **_kwargs: Any) -> None: ...

    Counter = Gauge = Histogram = _NoopMetric  # type: ignore[misc,assignment]
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    REGISTRY = None  # type: ignore[assignment]
    CollectorRegistry = None  # type: ignore[assignment,misc]

    def generate_latest(_registry: Any = None) -> bytes:  # type: ignore[misc]
        return b""


_INITIALISED = False


HTTP_REQUESTS_TOTAL = Counter(
    "zakuro_http_requests_total",
    "HTTP requests handled by the worker, by route + status + tenant.",
    labelnames=("method", "path", "status", "tenant_id"),
)


DISPATCH_LATENCY_SECONDS = Histogram(
    "zakuro_dispatch_latency_seconds",
    "End-to-end function dispatch latency (server-side handler wall clock).",
    labelnames=("worker_id", "tenant_id"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


WORKER_QUEUE_DEPTH = Gauge(
    "zakuro_worker_queue_depth",
    "Pending jobs in the worker's thread pool.",
    labelnames=("worker_id",),
)


AUTH_FAILURE_TOTAL = Counter(
    "zakuro_auth_failure_total",
    "Authentication or authorisation refusals, by reason (RFC 0002).",
    labelnames=("reason", "tenant_id"),
)


def init_metrics() -> bool:
    """Enable metric collection. Returns ``True`` iff prometheus_client is installed.

    Safe to call multiple times. Idempotent. Library code can call this at
    import time; callers can also call it directly.
    """
    global _INITIALISED
    if _INITIALISED:
        return _ENABLED
    _INITIALISED = True
    return _ENABLED


def is_enabled() -> bool:
    """Return True iff the optional dependency is installed."""
    return _ENABLED


def mount_metrics_endpoint(app: Any, *, path: str = "/metrics") -> None:
    """Mount ``GET <path>`` on the FastAPI ``app`` returning Prometheus exposition.

    No-op when ``prometheus_client`` is not installed — the endpoint is not
    added so a probe that hits it gets a 404, which is the right signal
    that metrics are off (rather than a degraded 200 with empty body).
    """
    if not _ENABLED:
        return
    from fastapi import Response

    @app.get(path, include_in_schema=False)
    def _metrics() -> Response:
        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )


def observe_dispatch_latency(worker_id: str, tenant_id: str) -> Callable[[], None]:
    """Return a context-manager-like callable that, when invoked, observes elapsed.

    Usage::

        stop = observe_dispatch_latency("w-1", "tenant-acme")
        try:
            run_job()
        finally:
            stop()
    """
    start = time.perf_counter()

    def _stop() -> None:
        elapsed = time.perf_counter() - start
        DISPATCH_LATENCY_SECONDS.labels(worker_id=worker_id, tenant_id=tenant_id).observe(elapsed)

    return _stop


def record_http_request(method: str, path: str, status: int, tenant_id: str) -> None:
    """Increment the HTTP-request counter once per handled request."""
    HTTP_REQUESTS_TOTAL.labels(
        method=method, path=path, status=str(status), tenant_id=tenant_id
    ).inc()


def record_auth_failure(reason: str, tenant_id: str = "") -> None:
    """Record an auth-layer refusal so SLO alerts can spot brute-force noise."""
    AUTH_FAILURE_TOTAL.labels(reason=reason, tenant_id=tenant_id).inc()


def set_worker_queue_depth(worker_id: str, depth: int) -> None:
    """Set the current queue-depth gauge for *worker_id*."""
    WORKER_QUEUE_DEPTH.labels(worker_id=worker_id).set(depth)


def _reset_for_tests() -> None:
    """Reset module-level init flag *and* clear all metric values. Test-only."""
    global _INITIALISED
    _INITIALISED = False
    if not _ENABLED:
        return
    # Zero the counters so test order doesn't matter.
    for metric in (
        HTTP_REQUESTS_TOTAL,
        DISPATCH_LATENCY_SECONDS,
        WORKER_QUEUE_DEPTH,
        AUTH_FAILURE_TOTAL,
    ):
        metric.clear()
