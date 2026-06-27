"""Security posture + fail-closed bind policy for the Zakuro worker.

Single source of truth for "is this worker safe to expose on the network?".
The worker's ``/execute`` boundary deserialises cloudpickle. Three independent
controls can authenticate the caller and make that safe:

* JWT auth   — ``ZAKURO_AUTH_REQUIRED=1`` (RFC 0002)
* HMAC wire  — ``ZAKURO_WIRE=v1`` plus a master key (RFC 0001 §"Step 5")
* mutual TLS — ``ZAKURO_CERT_DIR`` set (``cli.py`` then requires client certs)

When none is enabled, an ``/execute`` listener on a routable interface is an
unauthenticated remote-code-execution surface. :func:`enforce_bind_policy`
refuses such a bind to a non-loopback address unless the operator accepts the
risk with ``ZAKURO_INSECURE_BIND=1``. This mirrors the existing
``ZAKURO_TLS_REQUIRED`` fail-closed idiom in :mod:`zakuro.transport.tls`.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("zakuro.worker.posture")

_TRUE = {"1", "true", "yes"}
_FALSE = {"", "0", "false", "no"}
_WIRE_STRICT = {"v1"}
_WIRE_OFF = {"", "legacy", "off", "0", "none"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_DEFAULT_MAX_PAYLOAD = 256 * 1024 * 1024  # 256 MiB


class InsecureBindError(RuntimeError):
    """Raised when an insecure worker would bind a routable interface."""


class StartupConfigError(RuntimeError):
    """Raised when security-related env vars are inconsistent or typo'd."""


def is_auth_required() -> bool:
    """True iff ``ZAKURO_AUTH_REQUIRED`` is a recognised truthy value."""
    return os.environ.get("ZAKURO_AUTH_REQUIRED", "").strip().lower() in _TRUE


def is_wire_strict() -> bool:
    """Re-use the canonical predicate so the two never drift (DRY)."""
    from zakuro.worker.envelope import is_wire_strict as _impl

    return _impl()


def is_mtls_enabled() -> bool:
    """True iff a cert dir is configured — ``cli.py`` then requires client certs."""
    return bool(os.environ.get("ZAKURO_CERT_DIR", "").strip())


def is_caller_authenticated() -> bool:
    """True iff at least one control authenticates the ``/execute`` caller."""
    return is_auth_required() or is_wire_strict() or is_mtls_enabled()


def is_loopback(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK


def insecure_bind_allowed() -> bool:
    return os.environ.get("ZAKURO_INSECURE_BIND", "").strip().lower() in _TRUE


def max_payload_bytes() -> int:
    """Inbound ``/execute`` body cap. Raises :class:`StartupConfigError` if set
    to a non-positive / non-integer value."""
    raw = os.environ.get("ZAKURO_MAX_PAYLOAD_BYTES", "").strip()
    if not raw:
        return _DEFAULT_MAX_PAYLOAD
    try:
        val = int(raw)
    except ValueError as exc:
        raise StartupConfigError(
            f"ZAKURO_MAX_PAYLOAD_BYTES must be an integer, got {raw!r}"
        ) from exc
    if val <= 0:
        raise StartupConfigError("ZAKURO_MAX_PAYLOAD_BYTES must be positive")
    return val


def security_posture(host: str | None = None, port: int | None = None) -> dict[str, object]:
    """Structured snapshot of the worker's security posture for logging."""
    snap: dict[str, object] = {
        "auth_required": is_auth_required(),
        "wire_strict": is_wire_strict(),
        "mtls": is_mtls_enabled(),
        "caller_authenticated": is_caller_authenticated(),
        "max_payload_bytes": max_payload_bytes(),
    }
    if host is not None:
        snap["host"] = host
        snap["loopback"] = is_loopback(host)
    if port is not None:
        snap["port"] = port
    return snap


def validate_startup_config() -> None:
    """Fail fast on inconsistent security env vars.

    Raises :class:`StartupConfigError` when a security toggle is set to an
    unrecognised value (which would silently fail *open*), when the payload
    cap is malformed, or when strict wire is on without a usable master key.
    """
    wire = os.environ.get("ZAKURO_WIRE", "").strip().lower()
    if wire not in _WIRE_STRICT and wire not in _WIRE_OFF:
        raise StartupConfigError(
            f"ZAKURO_WIRE={wire!r} is not recognised. Use 'v1' for strict mode, "
            "or leave it unset for legacy mode — a typo here silently disables "
            "wire security."
        )

    auth = os.environ.get("ZAKURO_AUTH_REQUIRED", "").strip().lower()
    if auth not in _TRUE and auth not in _FALSE:
        raise StartupConfigError(
            f"ZAKURO_AUTH_REQUIRED={auth!r} is not recognised. Use '1' to require "
            "auth, or leave it unset to disable — a typo here silently disables auth."
        )

    # Eagerly validate the payload cap (raises StartupConfigError on garbage).
    max_payload_bytes()

    # Strict wire with no usable key must fail at boot, not on first request.
    if is_wire_strict():
        from zakuro.worker.envelope import EnvelopeRejectedError, _read_master_key

        try:
            _read_master_key()
        except EnvelopeRejectedError as exc:
            raise StartupConfigError(str(exc)) from exc
