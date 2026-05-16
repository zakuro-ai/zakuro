"""Observability surface for Zakuro.

Today this package exposes a single helper, ``init_sentry``, that the worker
entry point and the client wire into their startup paths. Future tickets
(OTel #123, Prometheus #124, structlog #125) will land here too so callers
have a single import surface to reason about.
"""

from zakuro.observability.sentry import init_sentry, set_request_context

__all__ = ["init_sentry", "set_request_context"]
