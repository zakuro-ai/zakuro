"""Observability surface for Zakuro.

Callers have a single import surface (``zakuro.observability``) to reason
about. Each helper is a no-op when its underlying optional dependency
is not installed, so applications can stay slim and ratchet observability
in by adding the extra (``pip install 'zakuro-ai[observability]'``).

Currently wired:

- ``init_sentry`` / ``set_request_context`` — error reporting (#128).
- ``init_logging`` / ``get_logger`` — structured JSON logs (#125, RFC 0003 Track A).
- ``init_metrics`` + ``mount_metrics_endpoint`` — Prometheus (#124, RFC 0003 Track B).

Forthcoming:

- ``init_tracing`` — OpenTelemetry tracing (#123).
"""

from zakuro.observability.logging import get_logger, init_logging
from zakuro.observability.metrics import (
    AUTH_FAILURE_TOTAL,
    DISPATCH_LATENCY_SECONDS,
    HTTP_REQUESTS_TOTAL,
    WORKER_QUEUE_DEPTH,
    init_metrics,
    mount_metrics_endpoint,
    observe_dispatch_latency,
    record_auth_failure,
    record_http_request,
    set_worker_queue_depth,
)
from zakuro.observability.metrics import (
    is_enabled as metrics_enabled,
)
from zakuro.observability.sentry import init_sentry, set_request_context

__all__ = [
    "AUTH_FAILURE_TOTAL",
    "DISPATCH_LATENCY_SECONDS",
    "HTTP_REQUESTS_TOTAL",
    "WORKER_QUEUE_DEPTH",
    "get_logger",
    "init_logging",
    "init_metrics",
    "init_sentry",
    "metrics_enabled",
    "mount_metrics_endpoint",
    "observe_dispatch_latency",
    "record_auth_failure",
    "record_http_request",
    "set_request_context",
    "set_worker_queue_depth",
]
