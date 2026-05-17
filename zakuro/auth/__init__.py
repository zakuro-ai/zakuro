"""Authentication and authorisation surface for Zakuro (#116, RFC 0002).

This package implements the request-side of the runtime's auth model:

- :func:`verify_jwt` — parse + verify an Ed25519-signed JWT issued by the
  broker, returning :class:`Claims` on success.
- :func:`derive_payload_hmac_key` — HKDF-derive the per-tenant key the
  worker uses to verify postcard-envelope HMACs (#117).
- :func:`require_jwt_scope` — FastAPI dependency that gates a handler on
  a specific JWT scope.

The transport-level mTLS layer lives in :mod:`zakuro.transport` (#115).
This package assumes mTLS has already verified the peer; JWTs add the
per-request authorisation on top.
"""

from zakuro.auth.jwt import (
    Claims,
    JwtError,
    JwtExpiredError,
    JwtScopeError,
    JwtSignatureError,
    derive_payload_hmac_key,
    verify_jwt,
)
from zakuro.auth.middleware import require_jwt_scope

__all__ = [
    "Claims",
    "JwtError",
    "JwtExpiredError",
    "JwtScopeError",
    "JwtSignatureError",
    "derive_payload_hmac_key",
    "require_jwt_scope",
    "verify_jwt",
]
