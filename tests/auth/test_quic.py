"""QUIC worker: frames honour ZAKURO_AUTH_REQUIRED (ADR 0013, #84)."""

from __future__ import annotations

import pytest

pytest.importorskip("aioquic")

import zakuro as zk  # noqa: E402
from tests.auth.conftest import issue_token  # noqa: E402
from zakuro.auth.frame import (  # noqa: E402
    AUTH_FLAG,
    FrameAuthError,
    authorize_frame,
    pack_token,
)

pytestmark = pytest.mark.timeout(30)


def test_frame_flag_unset_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    assert authorize_frame(1, b"abc") == (1, b"abc")
    # a token-carrying frame is accepted and stripped
    assert authorize_frame(1 | AUTH_FLAG, pack_token("t", b"abc")) == (1, b"abc")


def test_frame_requires_token(auth_env: bytes) -> None:
    with pytest.raises(FrameAuthError):
        authorize_frame(1, b"abc")
    with pytest.raises(FrameAuthError):
        authorize_frame(1 | AUTH_FLAG, b"\x00")  # truncated envelope
    with pytest.raises(FrameAuthError):
        authorize_frame(1 | AUTH_FLAG, pack_token("garbage", b"abc"))


def test_frame_scope_per_op(auth_env: bytes) -> None:
    tok = issue_token(auth_env, scopes=["exec:fn"])
    assert authorize_frame(1 | AUTH_FLAG, pack_token(tok, b"p")) == (1, b"p")
    with pytest.raises(FrameAuthError):
        authorize_frame(2 | AUTH_FLAG, pack_token(tok, b""))  # INFO needs worker:info
    assert authorize_frame(3, b"") == (3, b"")  # health open


def test_quic_roundtrip_unchanged_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAKURO_AUTH_REQUIRED", raising=False)
    with zk.Worker.spawn(transport="quic") as worker:

        @zk.fn
        def add(a: int, b: int) -> int:
            return a + b

        assert add.to(worker.compute(verify=False))(1, 2) == 3


def test_quic_end_to_end(auth_env: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # The spawned worker inherits the auth env; the client reads its token from ZAKURO_AUTH.
    with zk.Worker.spawn(transport="quic") as worker:

        @zk.fn
        def add(a: int, b: int) -> int:
            return a + b

        monkeypatch.delenv("ZAKURO_AUTH", raising=False)
        with pytest.raises(RuntimeError, match="protocol error"):
            add.to(worker.compute(verify=False))(1, 2)

        monkeypatch.setenv("ZAKURO_AUTH", issue_token(auth_env, scopes=["exec:fn"]))
        assert add.to(worker.compute(verify=False))(3, 4) == 7
