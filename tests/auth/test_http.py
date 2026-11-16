"""HTTP worker: /execute and /info honour ZAKURO_AUTH_REQUIRED (ADR 0013, #84)."""

from __future__ import annotations

import cloudpickle
import pytest
from fastapi.testclient import TestClient

from tests.auth.conftest import issue_token
from zakuro.worker.server import app


def _body() -> bytes:
    def f(x: int) -> int:
        return x * x

    return cloudpickle.dumps({"action": "execute", "func": f, "args": (6,), "kwargs": {}})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_flag_unset_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    with TestClient(app) as c:
        r = c.post("/execute", content=_body())
        assert r.status_code == 200
        assert cloudpickle.loads(r.content) == 36
        assert c.get("/info").status_code == 200


def test_no_token_rejected_before_execution(auth_env: bytes) -> None:
    with TestClient(app) as c:
        r = c.post("/execute", content=_body())
        assert r.status_code == 401
        assert r.json() == {"detail": ""}  # no reason leaked
        assert c.get("/info").status_code == 401


def test_bad_tokens_rejected(auth_env: bytes) -> None:
    with TestClient(app) as c:
        for token in (
            "garbage",
            issue_token(auth_env, scopes=["exec:fn"], exp_delta=-60),
            issue_token(auth_env, scopes=["worker:info"]),  # wrong scope
            issue_token(auth_env, scopes=["exec:fn"], worker_id="other"),
        ):
            assert c.post("/execute", content=_body(), headers=_bearer(token)).status_code == 401


def test_valid_token_executes(auth_env: bytes) -> None:
    with TestClient(app) as c:
        r = c.post(
            "/execute", content=_body(), headers=_bearer(issue_token(auth_env, scopes=["exec:fn"]))
        )
        assert r.status_code == 200
        assert cloudpickle.loads(r.content) == 36
        info = c.get("/info", headers=_bearer(issue_token(auth_env, scopes=["worker:info"])))
        assert info.status_code == 200


def test_health_stays_open(auth_env: bytes) -> None:
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_token_never_logged(auth_env: bytes, caplog: pytest.LogCaptureFixture) -> None:
    token = issue_token(auth_env, scopes=["exec:fn"], exp_delta=-60)
    with caplog.at_level("DEBUG"), TestClient(app) as c:
        c.post("/execute", content=_body(), headers=_bearer(token))
    assert token not in caplog.text
