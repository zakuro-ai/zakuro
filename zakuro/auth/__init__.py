"""Caller authentication for the Zakuro worker (ADR 0013, ``ZAKURO_AUTH_REQUIRED``).

Opt-in JWT bearer auth: the broker issues Ed25519-signed tokens, the worker
only verifies them. Off unless ``ZAKURO_AUTH_REQUIRED`` is truthy. Requires the
``[worker]`` extra (PyJWT with the ``crypto`` extra).

- :func:`verify_jwt` / :func:`authorize_token` -- verify a token (and scope).
- :func:`require_jwt_scope` -- FastAPI dependency gating an HTTP handler.
- :mod:`zakuro.auth.frame` -- the equivalent guard for QUIC frames.
"""

from zakuro.auth.jwt import (
    Claims,
    JwtError,
    JwtExpiredError,
    JwtScopeError,
    JwtSignatureError,
    authorize_token,
    verify_jwt,
)
from zakuro.auth.middleware import require_jwt_scope

__all__ = [
    "Claims",
    "JwtError",
    "JwtExpiredError",
    "JwtScopeError",
    "JwtSignatureError",
    "authorize_token",
    "require_jwt_scope",
    "verify_jwt",
]
