"""FastAPI test app shared across the auth-middleware tests.

The ``Depends(...)`` markers are passed as default-value rather than
via ``Annotated[..., Depends(...)]`` because nesting the function-style
build inside another function defeats FastAPI's annotation
introspection in some Python/FastAPI version combinations — the Depends
metadata in ``Annotated[].__metadata__`` is silently dropped, the
parameter becomes a regular query-string field, and every endpoint
returns 422.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from zakuro.auth import require_jwt_scope


def build_auth_app() -> FastAPI:
    app = FastAPI()

    exec_dep = require_jwt_scope("exec:fn")
    admin_dep = require_jwt_scope("admin:credits")

    @app.post("/execute")
    async def execute(claims=Depends(exec_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        return {"tenant": claims.tenant_id}

    @app.get("/admin")
    async def admin(claims=Depends(admin_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        return {"tenant": claims.tenant_id}

    return app
