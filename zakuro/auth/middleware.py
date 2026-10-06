"""FastAPI dependency that gates a handler on a required JWT scope (ADR 0013, #84).

Usage::

    @app.post("/execute", dependencies=[Depends(require_jwt_scope("exec:fn"))])

When ``ZAKURO_AUTH_REQUIRED`` is unset the dependency is a no-op and returns
anonymous claims (behaviour unchanged). When set, a missing/invalid token,
wrong audience/worker, expired token or missing scope is refused with an
empty-body HTTP 401 *before* the handler (and the body read) runs. The reason
is logged without the token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from zakuro.auth.jwt import (
    Claims,
    JwtError,
    JwtExpiredError,
    JwtScopeError,
    JwtSignatureError,
    authorize_token,
)
from zakuro.worker.posture import is_auth_required

logger = logging.getLogger(__name__)

ANONYMOUS_CLAIMS = Claims(
    tenant_id="anonymous",
    worker_id="",
    scopes=frozenset(),
    jti="",
    exp=0,
    nbf=0,
    aud="",
)


def failure_reason(exc: JwtError) -> str:
    """Coarse, token-free label for an auth failure (for logs)."""
    if isinstance(exc, JwtExpiredError):
        return "expired"
    if isinstance(exc, JwtSignatureError):
        return "signature"
    if isinstance(exc, JwtScopeError):
        return "scope_denied"
    return "malformed"


def require_jwt_scope(required_scope: str) -> Callable[..., Any]:
    """Return a FastAPI dependency asserting the bearer JWT carries *required_scope*."""

    async def _dep(request: Request) -> Claims:
        if not is_auth_required():
            return ANONYMOUS_CLAIMS
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            logger.warning("auth refused: missing_bearer")
            raise HTTPException(status_code=401, detail="")
        token = auth_header.split(" ", 1)[1].strip()
        try:
            return authorize_token(token, required_scope)
        except JwtError as exc:
            logger.warning("auth refused: %s", failure_reason(exc))
            raise HTTPException(status_code=401, detail="") from None

    return _dep


__all__ = ["ANONYMOUS_CLAIMS", "failure_reason", "require_jwt_scope"]
