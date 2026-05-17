"""Observability surface for Zakuro.

Callers have a single import surface (``zakuro.observability``) to reason
about. Each helper is a no-op when its underlying optional dependency
is not installed, so applications can stay slim and ratchet observability
in by adding the extra (``pip install 'zakuro-ai[observability]'``).

Currently wired:

- ``init_sentry`` / ``set_request_context`` — error reporting (#128).
- ``init_logging`` / ``get_logger`` — structured JSON logs (#125, RFC 0003 Track A).

Forthcoming:

- ``init_tracing`` — OpenTelemetry tracing (#123).
- ``metrics`` namespace — Prometheus counters/histograms (#124).
"""

from zakuro.observability.logging import get_logger, init_logging
from zakuro.observability.sentry import init_sentry, set_request_context

__all__ = [
    "get_logger",
    "init_logging",
    "init_sentry",
    "set_request_context",
]
