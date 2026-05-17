"""Structured-JSON logging for Zakuro (closes #125 — RFC 0003 Track A).

Design notes:

* The single entry point is :func:`init_logging`. It configures structlog
  *and* the stdlib :mod:`logging` module so that downstream library calls
  (``logger = logging.getLogger(__name__)``) produce the same JSON shape
  as direct ``structlog.get_logger(...).info(...)`` calls.
* When ``structlog`` is not installed, :func:`init_logging` is a no-op
  that returns ``False``. The library still imports cleanly. This keeps
  the observability extra truly opt-in (matches :func:`init_sentry`).
* The PII redactor from :mod:`zakuro.observability.sentry` is reused as
  a structlog processor — the same regexes that strip emails / IPs / home
  paths from Sentry events also strip them from log records before they
  leave the process.
* The shared ``_REQUEST_CONTEXT`` ContextVar (also from
  :mod:`zakuro.observability.sentry`) is read by a structlog processor
  so every log line in a request scope automatically carries
  ``request-id`` / ``tenant-id`` / ``worker-id``.

Stable JSON schema (one record per line)::

    {
      "time":      "2026-05-17T14:23:08.412Z",
      "level":     "info",
      "component": "worker",
      "event":     "job_completed",
      "request-id":"req-0c12...",
      "tenant-id": "tenant-acme",
      "worker-id": "worker-3",
      "duration_ms": 142
    }

Keys ``time``, ``level``, ``component``, ``event`` are guaranteed stable
across versions; the request-context tags are present iff
:func:`set_request_context` ran in the caller's async scope. See
``docs/STABILITY.md`` § "Log schema".
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from zakuro.observability.sentry import _REQUEST_CONTEXT, _redact_pii_in_string

try:  # structlog is an optional dependency
    import structlog
except ImportError:  # pragma: no cover - exercised only without the extra
    structlog = None  # type: ignore[assignment]


_INITIALISED = False


def init_logging(component: str, *, level: str | None = None) -> bool:
    """Initialise structured JSON logging for *component*.

    *component* is one of ``"worker"`` / ``"client"`` / ``"broker"`` and
    appears on every record so log aggregators can filter by source.

    *level* overrides the level resolved from ``ZAKURO_LOG_LEVEL`` (env)
    or defaults to ``"INFO"``. Pass a stdlib level name (``"DEBUG"`` /
    ``"INFO"`` / ``"WARNING"`` / ``"ERROR"``).

    Returns ``True`` if structlog was wired in, ``False`` if the optional
    dependency is missing (in which case stdlib logging is left alone so
    the caller still gets *some* output).

    Safe to call multiple times — subsequent calls are no-ops, so library
    code can wire it at import time without worrying about being called
    twice from the entry point.
    """
    global _INITIALISED
    if _INITIALISED:
        return structlog is not None
    if structlog is None:
        return False

    resolved_level = (level or os.environ.get("ZAKURO_LOG_LEVEL") or "INFO").upper()
    numeric_level = logging.getLevelName(resolved_level)
    if not isinstance(numeric_level, int):
        # logging.getLevelName returns the string back when the name is
        # unknown — fall through to INFO rather than passing the string.
        numeric_level = logging.INFO

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="time"),
        _attach_component(component),
        _attach_request_context,
        _redact_pii_processor,
    ]

    # Configure the stdlib logging root so non-structlog callers (third-
    # party libs, framework code) emit the same JSON shape.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Replace the stdlib handlers' formatters so logger.info(...) from
    # third-party libraries flows through structlog's processor chain too.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

    _INITIALISED = True
    return True


def get_logger(name: str | None = None) -> Any:
    """Return a structlog bound logger, or a stdlib logger as fallback.

    Library code should prefer ``get_logger(__name__)`` so a single
    import line works whether or not :func:`init_logging` has run.
    """
    if structlog is None:
        return logging.getLogger(name)
    return structlog.get_logger(name)


def _attach_component(component: str) -> Any:
    """Return a structlog processor that stamps ``component`` on every record."""

    def _processor(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("component", component)
        return event_dict

    return _processor


def _attach_request_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Pull request-id / tenant-id / worker-id from the shared ContextVar."""
    for key, value in _REQUEST_CONTEXT.get().items():
        if value:
            event_dict.setdefault(key, value)
    return event_dict


# Keys whose values are produced by the log infrastructure itself and are
# known-safe — redacting them would corrupt the schema (the loose IPv6 regex
# happily matches "01:02:03" timestamps).
_INFRASTRUCTURE_KEYS = frozenset(
    {"time", "timestamp", "level", "component", "event", "logger", "logger_name"}
)


def _redact_pii_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Walk every string-valued user field and redact emails / IPs / home paths."""
    for key, value in list(event_dict.items()):
        if key in _INFRASTRUCTURE_KEYS:
            continue
        if isinstance(value, str):
            event_dict[key] = _redact_pii_in_string(value)
    return event_dict


def _reset_for_tests() -> None:
    """Reset the module-level init flag. Test-only — do not call in prod."""
    global _INITIALISED
    _INITIALISED = False
