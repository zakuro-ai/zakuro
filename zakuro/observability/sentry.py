"""Sentry SDK initialisation for Zakuro (closes #128).

Design notes:

* Sample rate is fixed at the values the issue specifies — 10% of errors
  reach Sentry, performance (tracing) is disabled because OpenTelemetry
  (#123) owns request-level tracing.
* The DSN is read from ``SENTRY_DSN`` in the environment. When no DSN is
  set, ``init_sentry`` is a no-op and returns ``False``. That keeps Sentry
  truly opt-in: a worker without the env var behaves identically to today.
* The ``_before_send`` hook scrubs PII before any event leaves the
  process. The scrubber has its own unit tests in
  ``tests/observability/test_sentry_scrubber.py``.
* Per-event tags ``request-id`` / ``tenant-id`` / ``worker-id`` are read
  from a single ``ContextVar`` so that async request handlers can attach
  them at the boundary without leaking into siblings.

When the secrets refactor (#118) lands, swap the ``os.environ`` lookup for
the unified secret-provider call.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

try:  # sentry-sdk is an optional dependency
    import sentry_sdk
except ImportError:  # pragma: no cover - exercised only without the extra
    sentry_sdk = None  # type: ignore[assignment]


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b[A-Fa-f0-9:]*:[A-Fa-f0-9:]+\b")  # loose, intentional
_HOME_PATH_RE = re.compile(r"(/Users|/home)/[^/]+")
_SENSITIVE_HEADER_KEYS = frozenset(
    {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
)
_REDACTED = "<redacted>"
_EMPTY_CONTEXT: Mapping[str, str] = {}
_REQUEST_CONTEXT: ContextVar[Mapping[str, str]] = ContextVar(
    "_zakuro_sentry_request_context", default=_EMPTY_CONTEXT
)


def init_sentry(component: str, *, dsn: str | None = None) -> bool:
    """Initialise the Sentry SDK for *component* (``"worker"`` / ``"client"``).

    Returns ``True`` if Sentry was actually initialised, ``False`` if there
    is no DSN configured (the expected production behaviour for components
    that opt out) or if ``sentry-sdk`` is not installed.
    """
    if sentry_sdk is None:
        return False
    resolved_dsn = dsn or os.environ.get("SENTRY_DSN")
    if not resolved_dsn:
        return False

    sentry_sdk.init(
        dsn=resolved_dsn,
        sample_rate=0.1,  # 10% of errors; per #128
        traces_sample_rate=0.0,  # OTel handles tracing (#123)
        send_default_pii=False,  # belt-and-braces; the scrubber is the rope
        before_send=_before_send,  # type: ignore[arg-type]  # SDK's Event TypedDict; we treat event as plain dict
        environment=os.environ.get("ZAKURO_ENV", "production"),
        release=os.environ.get("ZAKURO_RELEASE"),
    )
    scope_tags = {
        "component": component,
        "worker-id": os.environ.get("ZAKURO_WORKER_ID", ""),
    }
    sentry_sdk.set_tag("component", component)
    if scope_tags["worker-id"]:
        sentry_sdk.set_tag("worker-id", scope_tags["worker-id"])
    return True


def set_request_context(
    *,
    request_id: str | None = None,
    tenant_id: str | None = None,
    worker_id: str | None = None,
) -> None:
    """Attach per-request tags so the next Sentry event carries them.

    Call this at the request-handling boundary (HTTP middleware, QUIC
    handler entry). Subsequent ``sentry_sdk.capture_exception`` /
    automatic captures within the same async context will pick the tags
    up via :func:`_before_send`.
    """
    current = dict(_REQUEST_CONTEXT.get())
    if request_id is not None:
        current["request-id"] = request_id
    if tenant_id is not None:
        current["tenant-id"] = tenant_id
    if worker_id is not None:
        current["worker-id"] = worker_id
    _REQUEST_CONTEXT.set(current)


def _before_send(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Sentry ``before_send`` hook — strip PII, attach request tags.

    Exposed at module level so it is import-safe and so the unit tests
    can call it without booting the Sentry SDK.
    """
    _scrub_user_fields(event)
    _scrub_request(event)
    _scrub_strings_in_place(event)

    tags = event.setdefault("tags", {})
    for key, value in _REQUEST_CONTEXT.get().items():
        if value:
            tags[key] = value
    return event


def _scrub_user_fields(event: dict[str, Any]) -> None:
    user = event.get("user")
    if isinstance(user, dict):
        for key in ("email", "ip_address", "username", "name"):
            user.pop(key, None)


def _scrub_request(event: dict[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    # Request body may carry user data: drop it entirely rather than try
    # to be clever about its schema.
    request.pop("data", None)
    request.pop("query_string", None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        for key in list(headers.keys()):
            if key.lower() in _SENSITIVE_HEADER_KEYS:
                headers[key] = _REDACTED


def _scrub_strings_in_place(node: Any) -> None:
    """Walk the event tree and rewrite any string fields with PII redacted."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                node[key] = _redact_pii_in_string(value)
            else:
                _scrub_strings_in_place(value)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                node[index] = _redact_pii_in_string(value)
            else:
                _scrub_strings_in_place(value)


def _redact_pii_in_string(value: str) -> str:
    value = _EMAIL_RE.sub("<email>", value)
    value = _IPV4_RE.sub("<ip>", value)
    value = _IPV6_RE.sub("<ip>", value)
    value = _HOME_PATH_RE.sub(lambda m: f"{m.group(1)}/<user>", value)
    return value
