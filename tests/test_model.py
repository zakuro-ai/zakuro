"""Tests for zakuro.model — the zk.model(uri).chat(...) SDK surface."""

from __future__ import annotations

import httpx
import pytest

import zakuro as zk
from zakuro.model import ChatResult, Model, ModelInferenceError

VALID_UUID = "12345678-1234-5678-1234-567812345678"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def test_chat_posts_to_infer_and_returns_chat_result(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(
            200,
            {
                "content": "hello there",
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                "provider": "worker-abc",
                "charged_zkcr": 0.0042,
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    m = zk.model(f"zc://{VALID_UUID}", broker="http://broker.example:9000")
    result = m.chat([{"role": "user", "content": "hi"}])

    assert captured["url"] == "http://broker.example:9000/infer"
    assert captured["json"] == {
        "model": f"zc://{VALID_UUID}",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 256,
    }

    assert isinstance(result, ChatResult)
    assert result.content == "hello there"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert result.provider == "worker-abc"
    assert result.charged_zkcr == 0.0042


@pytest.mark.parametrize(
    "bad_uri",
    ["zc://node-abc", "not-a-uuid", "", "zc://"],
)
def test_invalid_model_uri_raises_value_error(bad_uri):
    with pytest.raises(ValueError):
        zk.model(bad_uri)


@pytest.mark.parametrize(
    "uri",
    [VALID_UUID, f"zc://{VALID_UUID}", f"zc://model-{VALID_UUID}"],
)
def test_valid_model_uri_forms_accepted(uri):
    m = zk.model(uri, broker="http://broker.example:9000")
    assert m.uri == uri


def test_404_error_response_surfaces_server_message(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(404, {"error": "no provider serving zc://" + VALID_UUID})

    monkeypatch.setattr(httpx, "post", fake_post)

    m = zk.model(VALID_UUID, broker="http://broker.example:9000")
    with pytest.raises(ModelInferenceError, match="no provider serving"):
        m.chat([{"role": "user", "content": "hi"}])


def test_broker_override_used_verbatim():
    m = Model(VALID_UUID, broker="http://custom-broker:1234/")
    assert m.broker_url == "http://custom-broker:1234"


def test_default_broker_resolved_from_config(monkeypatch):
    from zakuro.config import Config

    def fake_load():
        return Config(default_host="my.zakuro-ai.com", default_port=9000)

    monkeypatch.setattr(Config, "load", staticmethod(fake_load))

    m = Model(VALID_UUID)
    assert m.broker_url == "http://my.zakuro-ai.com:9000"


def test_api_key_argument_sent_as_bearer(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, {"content": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
    zk.model(VALID_UUID, broker="http://b:9000", api_key="zk_1_secret").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert captured["headers"] == {"Authorization": "Bearer zk_1_secret"}


def test_api_key_env_fallback(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, {"content": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_2_envkey")
    zk.model(VALID_UUID, broker="http://b:9000").chat([{"role": "user", "content": "hi"}])
    assert captured["headers"] == {"Authorization": "Bearer zk_2_envkey"}


def test_no_api_key_sends_no_auth_header(monkeypatch):
    # A keyless local broker must keep working exactly as before.
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, {"content": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
    zk.model(VALID_UUID, broker="http://b:9000").chat([{"role": "user", "content": "hi"}])
    assert captured["headers"] == {}
