"""Security posture for the Zakuro worker.

Single source of truth for worker security knobs. This module is the first
slice of the staged port of the ``develop`` security stack (ADR 0013); later
slices add the auth/wire/mTLS predicates and the fail-closed bind policy.

Currently provides the inbound ``/execute`` payload cap, enforced by the HTTP
and QUIC servers *before* a body is buffered.
"""

from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "yes", "on"})
_DEFAULT_MAX_PAYLOAD = 256 * 1024 * 1024  # 256 MiB


class StartupConfigError(RuntimeError):
    """Raised when security-related env vars are inconsistent or typo'd."""


def max_payload_bytes() -> int:
    """Inbound ``/execute`` body cap (``ZAKURO_MAX_PAYLOAD_BYTES``, default 256 MiB).

    Raises :class:`StartupConfigError` if set to a non-positive or non-integer value.
    """
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


def is_auth_required() -> bool:
    """True iff ``ZAKURO_AUTH_REQUIRED`` is truthy: ``/execute`` needs a JWT (ADR 0013)."""
    return os.environ.get("ZAKURO_AUTH_REQUIRED", "").strip().lower() in _TRUE


def validate_auth_config() -> None:
    """Fail fast at startup if JWT auth is on but cannot work.

    No-op when ``ZAKURO_AUTH_REQUIRED`` is unset. Raises ``ImportError`` if
    PyJWT/cryptography (``[worker]`` extra) is missing and
    :class:`StartupConfigError` if no verification key is configured.
    """
    if not is_auth_required():
        return
    import zakuro.auth  # noqa: F401  (raises a clear ImportError if PyJWT is missing)

    if not os.environ.get("ZAKURO_JWT_PUBLIC_KEY_PEM", "").strip():
        raise StartupConfigError(
            "ZAKURO_AUTH_REQUIRED is set but ZAKURO_JWT_PUBLIC_KEY_PEM is not: "
            "the worker cannot verify any token"
        )


def validate_wire_config() -> None:
    """Fail fast at startup if ``ZAKURO_WIRE=v1|v2`` has no usable HMAC key (ZAK-0013)."""
    from zakuro.wire.keys import WireKeyError, is_wire_strict, read_master_key

    if not is_wire_strict():
        return
    try:
        read_master_key()
    except WireKeyError as exc:
        raise StartupConfigError(str(exc)) from None
