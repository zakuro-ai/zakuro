"""FastAPI dependency that gates a handler on a required JWT scope (#116, RFC 0002).

Usage in handlers::

    from fastapi import Depends
    from zakuro.auth import require_jwt_scope, Claims

    @app.post("/execute")
    async def execute(claims: Claims = Depends(require_jwt_scope("exec:fn"))):
        ...

The dependency picks the worker's identity from
``ZAKURO_WORKER_ID`` so a leaked token issued for a sibling worker is
rejected before any handler runs. Set ``ZAKURO_AUTH_REQUIRED=1`` in the
environment to enforce; when unset, the dependency returns
``Claims(... scopes=set(), tenant_id="anonymous")`` — that's the
opt-in rollout switch from RFC 0002 §"Step 5 — flip strict".

Observability
-------------

Every auth refusal increments the
``zakuro_auth_failure_total{reason,tenant_id}`` Prometheus counter so
operations can spot brute-force patterns (#124, RFC 0003 Track B).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

try:
    from fastapi import HTTPException, Request
except ImportError as exc:  # pragma: no cover
    raise ImportError("JWT middleware requires the [worker] extra (fastapi).") from exc

from zakuro.auth.jwt import (
    Claims,
    JwtError,
    JwtExpiredError,
    JwtScopeError,
    JwtSignatureError,
    verify_jwt,
)

_ANONYMOUS_CLAIMS = Claims(
    tenant_id="anonymous",
    worker_id="",
    scopes=frozenset(),
    jti="",
    exp=0,
    nbf=0,
    aud="",
)


def _auth_required() -> bool:
    return os.environ.get("ZAKURO_AUTH_REQUIRED", "").strip() in {"1", "true", "yes"}


def _record_failure(reason: str, tenant_id: str = "") -> None:
    """Increment the auth-failure metric — no-op when Prometheus is missing.

    The import is deferred + caught because :mod:`zakuro.observability.metrics`
    ships in PR #185 (RFC 0003 Track B). When this PR lands before that one,
    auth still works; metrics just stay at zero until the metrics module
    is importable.
    """
    try:
        from zakuro.observability.metrics import record_auth_failure
    except ImportError:  # pragma: no cover
        return
    record_auth_failure(reason, tenant_id)


def require_jwt_scope(required_scope: str) -> Callable[..., Any]:
    """Return a FastAPI dependency that asserts the JWT carries *required_scope*.

    The dependency:

      1. extracts the bearer token from ``Authorization``;
      2. verifies the signature, audience, expiry, nbf;
      3. checks the token's ``scopes`` includes ``required_scope``;
      4. checks the token's ``worker_id`` matches this worker.

    Failures raise HTTP 401 with no body — leaking the reason would aid
    enumeration. The reason is recorded in
    ``zakuro_auth_failure_total`` so operators can still triage.

    When ``ZAKURO_AUTH_REQUIRED`` is unset or falsy, the dependency
    returns anonymous claims so the handler can still inspect
    ``claims.tenant_id == "anonymous"`` if it wants to refuse anyway.
    This is the v0.3 → v0.4 rollout switch (see RFC 0002 §"Step 5").
    """

    async def _dep(request: Request) -> Claims:
        if not _auth_required():
            return _ANONYMOUS_CLAIMS
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            _record_failure("missing_bearer")
            raise HTTPException(status_code=401, detail="")
        token = auth_header.split(" ", 1)[1].strip()
        worker_id = os.environ.get("ZAKURO_WORKER_ID") or None
        audience = os.environ.get("ZAKURO_JWT_AUDIENCE", "zakuro-worker")
        try:
            claims = verify_jwt(
                token,
                expected_audience=audience,
                expected_worker_id=worker_id,
                leeway_seconds=int(os.environ.get("ZAKURO_JWT_LEEWAY_SECONDS", "5")),
            )
        except JwtExpiredError:
            _record_failure("expired")
            raise HTTPException(status_code=401, detail="") from None
        except JwtSignatureError as exc:
            _record_failure("signature")
            raise HTTPException(status_code=401, detail="") from exc
        except JwtError as exc:
            _record_failure("malformed")
            raise HTTPException(status_code=401, detail="") from exc

        if not claims.has_scope(required_scope):
            _record_failure("scope_denied", claims.tenant_id)
            raise HTTPException(status_code=401, detail="") from None
        return claims

    return _dep


# Re-exported so callers can `from zakuro.auth import JwtScopeError`.
__all__ = ["require_jwt_scope", "JwtScopeError"]
