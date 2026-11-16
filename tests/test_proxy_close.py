"""RemoteProxy.close() / context manager send release_instance (best-effort)."""

from __future__ import annotations

from typing import Any

import cloudpickle
import pytest

from zakuro.compute import Compute
from zakuro.proxy import RemoteProxy


class Thing:
    def ping(self) -> str:
        return "pong"


class Closable:
    def close(self) -> None:
        pass


def _make(
    monkeypatch: pytest.MonkeyPatch,
    reply: Any = None,
    raises: Exception | None = None,
    klass: type = Thing,
    close_reply: Any = None,
):
    sent: list[tuple[str, dict[str, Any], str | None]] = []

    def fake_send(self: RemoteProxy, payload: bytes, action: str, iid: str | None) -> bytes:
        sent.append((action, cloudpickle.loads(payload), iid))
        if action == "create_instance":
            return cloudpickle.dumps({"instance_id": iid})
        if action == "call_method":
            return cloudpickle.dumps(close_reply)
        if raises is not None:
            raise raises
        return cloudpickle.dumps(reply if reply is not None else {"released": True})

    monkeypatch.setattr(RemoteProxy, "_send", fake_send)
    proxy = RemoteProxy(klass, (), {}, Compute(uri="zakuro://127.0.0.1:1", verify=False))
    return proxy, sent


def test_close_sends_release_once(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, sent = _make(monkeypatch)
    proxy.close()
    proxy.close()
    releases = [s for s in sent if s[0] == "release_instance"]
    assert len(releases) == 1
    assert releases[0][1]["instance_id"] == proxy._instance_id
    assert releases[0][2] == proxy._instance_id


def test_context_manager_releases_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, sent = _make(monkeypatch)
    with proxy as p:
        assert p is proxy
    assert [s[0] for s in sent] == ["create_instance", "release_instance"]


def test_context_manager_releases_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, sent = _make(monkeypatch)
    with pytest.raises(ValueError), proxy:
        raise ValueError("boom")
    assert sent[-1][0] == "release_instance"


def test_close_evicted_instance_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    err = RuntimeError("Instance not found: x (expired or evicted)")
    proxy, _ = _make(monkeypatch, reply=err)
    proxy.close()


def test_close_swallows_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, _ = _make(monkeypatch, raises=ConnectionError("down"))
    proxy.close()
    proxy.close()


def test_class_without_close_only_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, sent = _make(monkeypatch)
    proxy.close()
    assert [s[0] for s in sent] == ["create_instance", "release_instance"]


def test_class_with_close_calls_it_then_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, sent = _make(monkeypatch, klass=Closable)
    proxy.close()
    assert [s[0] for s in sent] == ["create_instance", "call_method", "release_instance"]
    assert sent[1][1]["method"] == "close"


def test_user_close_error_propagates_after_release(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, sent = _make(monkeypatch, klass=Closable, close_reply=RuntimeError("user close failed"))
    with pytest.raises(RuntimeError, match="user close failed"):
        proxy.close()
    assert sent[-1][0] == "release_instance"
    n = len(sent)
    proxy.close()
    assert len(sent) == n


def test_second_close_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, sent = _make(monkeypatch, klass=Closable)
    proxy.close()
    n = len(sent)
    proxy.close()
    assert len(sent) == n
