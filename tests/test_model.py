"""Tests for zakuro.model — the zk.model(uri).chat(...)/.transcribe(...) SDK surface."""

from __future__ import annotations

import httpx
import pytest

import zakuro as zk
from zakuro.model import ChatResult, Model, ModelInferenceError, TranscriptResult

VALID_UUID = "12345678-1234-5678-1234-567812345678"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ZAKURO_BROKER", "ZAKURO_API_URL", "ZAKURO_ENV", "ZAKURO_HUB_URL"):
        monkeypatch.delenv(var, raising=False)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _FakeTranscribeResponse:
    """Fake response for the multipart files=/data= transcribe call."""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _transcribe_payload(**overrides):
    payload = {
        "text": "hello there",
        "language": "shona",
        "duration_s": 10.96,
        "billed_seconds": 11,
        "truncated": False,
        "charged": "0.165000",
        "balance": "2999476.284900",
    }
    payload.update(overrides)
    return payload


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
                "charged": 0.63,
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
    assert result.charged == 0.63


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
        return Config(default_host="broker.example.com", default_port=9000)

    monkeypatch.setattr(Config, "load", staticmethod(fake_load))

    m = Model(VALID_UUID)
    assert m.broker_url == "http://broker.example.com:9000"


def test_api_key_argument_sent_as_bearer(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, {"content": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
    zk.model(VALID_UUID, broker="http://127.0.0.1:9000", api_key="zk_1_secret").chat(
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
    zk.model(VALID_UUID, broker="http://127.0.0.1:9000").chat([{"role": "user", "content": "hi"}])
    assert captured["headers"] == {"Authorization": "Bearer zk_2_envkey"}


def test_no_api_key_sends_no_auth_header(monkeypatch):
    # A keyless local broker must keep working exactly as before.
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, {"content": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
    zk.model(VALID_UUID, broker="http://127.0.0.1:9000").chat([{"role": "user", "content": "hi"}])
    assert captured["headers"] == {}


# --- transcribe() -----------------------------------------------------------


def test_transcribe_posts_to_hub_and_returns_transcript_result(tmp_path, monkeypatch):
    captured = {}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured["url"] = url
        filename, stream = files["file"]
        captured["filename"] = filename
        # Read here, where real httpx reads: the file part is an open handle,
        # not bytes, so that a clip streams instead of being held in memory.
        # By the time this test asserts, transcribe's `with` has closed it.
        captured["content"] = stream.read()
        captured["data"] = data
        return _FakeTranscribeResponse(200, _transcribe_payload())

    monkeypatch.setattr(httpx, "post", fake_post)

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"fake-audio-bytes")

    m = zk.model(f"zc://{VALID_UUID}", hub="http://hub.example")
    result = m.transcribe(str(clip))

    assert captured["url"] == f"http://hub.example/api/models/{VALID_UUID}/transcribe"
    assert captured["filename"] == "clip.wav"
    assert captured["content"] == b"fake-audio-bytes"
    assert "language" not in captured["data"]

    assert isinstance(result, TranscriptResult)
    assert result.text == "hello there"
    assert result.language == "shona"
    assert result.duration_s == 10.96
    assert result.billed_seconds == 11
    assert result.truncated is False
    assert result.charged == 0.165
    assert result.balance == 2999476.2849


@pytest.mark.parametrize(
    "uri",
    [VALID_UUID, f"zc://{VALID_UUID}", f"zc://model-{VALID_UUID}"],
)
def test_transcribe_uri_forms_all_produce_bare_uuid_url(tmp_path, monkeypatch, uri):
    captured = {}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeTranscribeResponse(200, _transcribe_payload())

    monkeypatch.setattr(httpx, "post", fake_post)

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    zk.model(uri, hub="http://hub.example").transcribe(str(clip))
    assert captured["url"] == f"http://hub.example/api/models/{VALID_UUID}/transcribe"


def test_transcribe_language_sent_when_given(tmp_path, monkeypatch):
    captured = {}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured["data"] = data
        return _FakeTranscribeResponse(200, _transcribe_payload())

    monkeypatch.setattr(httpx, "post", fake_post)

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    zk.model(VALID_UUID, hub="http://hub.example").transcribe(str(clip), language="sn")
    assert captured["data"]["language"] == "sn"


def test_transcribe_accepts_path_str_and_file_object(tmp_path, monkeypatch):
    captured = []

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured.append(files["file"][0])
        return _FakeTranscribeResponse(200, _transcribe_payload())

    monkeypatch.setattr(httpx, "post", fake_post)

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    m = zk.model(VALID_UUID, hub="http://hub.example")

    m.transcribe(clip)  # pathlib.Path
    m.transcribe(str(clip))  # str
    with open(clip, "rb") as fh:  # open binary file object
        m.transcribe(fh)

    assert captured == ["clip.wav", "clip.wav", "clip.wav"]


def test_transcribe_api_key_argument_sent_as_bearer(tmp_path, monkeypatch):
    captured = {}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeTranscribeResponse(200, _transcribe_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    zk.model(VALID_UUID, hub="https://hub.example", api_key="zk_1_secret").transcribe(str(clip))
    assert captured["headers"] == {"Authorization": "Bearer zk_1_secret"}


def test_transcribe_api_key_env_fallback(tmp_path, monkeypatch):
    captured = {}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeTranscribeResponse(200, _transcribe_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_2_envkey")

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    zk.model(VALID_UUID, hub="https://hub.example").transcribe(str(clip))
    assert captured["headers"] == {"Authorization": "Bearer zk_2_envkey"}


def test_transcribe_hub_from_hub_argument(tmp_path, monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeTranscribeResponse(200, _transcribe_payload())
    )
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    m = zk.model(VALID_UUID, hub="http://custom-hub:1234/")
    assert m.hub_url == "http://custom-hub:1234"
    m.transcribe(str(clip))


def test_transcribe_hub_resolved_from_config(monkeypatch, tmp_path):
    from zakuro.config import Config

    def fake_load():
        return Config(hub_url="http://configured-hub.example")

    monkeypatch.setattr(Config, "load", staticmethod(fake_load))

    m = Model(VALID_UUID)
    assert m.hub_url == "http://configured-hub.example"


def test_transcribe_truncated_true_is_surfaced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeTranscribeResponse(200, _transcribe_payload(truncated=True)),
    )
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    result = zk.model(VALID_UUID, hub="http://hub.example").transcribe(str(clip))
    assert result.truncated is True


@pytest.mark.parametrize(
    "status,detail",
    [
        (402, {"errors": ["insufficient balance"]}),
        (404, {"errors": ["model not visible to this key"]}),
        (409, {"errors": ["no committed runner"]}),
        (413, {"errors": ["file exceeds 25 MB"]}),
        (422, {"errors": ["audio could not be decoded"]}),
        (503, {"errors": ["runner unavailable"]}),
    ],
)
def test_transcribe_error_statuses_raise_with_hub_message(tmp_path, monkeypatch, status, detail):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeTranscribeResponse(status, {"detail": detail})
    )
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    with pytest.raises(ModelInferenceError, match=detail["errors"][0]):
        zk.model(VALID_UUID, hub="http://hub.example").transcribe(str(clip))


def test_transcribe_413_raises_with_file_too_large_message(tmp_path, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeTranscribeResponse(
            413, {"detail": {"errors": ["file exceeds 25 MB limit"]}}
        ),
    )
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    with pytest.raises(ModelInferenceError, match="file exceeds 25 MB limit"):
        zk.model(VALID_UUID, hub="http://hub.example").transcribe(str(clip))


def test_transcribe_422_raises_with_decode_error_message(tmp_path, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeTranscribeResponse(
            422, {"detail": {"errors": ["audio could not be decoded"]}}
        ),
    )
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    with pytest.raises(ModelInferenceError, match="audio could not be decoded"):
        zk.model(VALID_UUID, hub="http://hub.example").transcribe(str(clip))


def test_transcribe_transport_failure_raises_model_inference_error(tmp_path, monkeypatch):
    def fake_post(*a, **k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")

    with pytest.raises(ModelInferenceError):
        zk.model(VALID_UUID, hub="http://hub.example").transcribe(str(clip))


def test_extract_error_message_still_handles_broker_error_shape():
    """The shared helper must not regress the broker's {"error": "..."} shape."""
    from zakuro.model import _extract_error_message

    response = _FakeResponse(404, {"error": "no provider serving " + VALID_UUID})
    assert _extract_error_message(response) == "no provider serving " + VALID_UUID


# --- hub default for chat ---------------------------------------------------


def _capture_post(monkeypatch, response):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def test_chat_defaults_to_hub_infer(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_hub_key")
    captured = _capture_post(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "content": "hi!",
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "charged": "0.045000",
                "balance": "1500.0",
            },
        ),
    )
    result = zk.model(f"zc://{VALID_UUID}").chat(
        [{"role": "user", "content": "hello"}], max_tokens=64
    )

    assert captured["url"] == f"https://hub.zakuro-ai.com/api/models/{VALID_UUID}/infer"
    assert captured["headers"] == {"Authorization": "Bearer zk_hub_key"}
    assert captured["json"] == {
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 64,
    }
    assert result.content == "hi!"
    assert result.usage["total_tokens"] == 3
    assert result.charged == pytest.approx(0.045)
    assert isinstance(result.charged, float)
    assert result.provider == "hub"


def test_chat_hub_env_staging(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_k")
    monkeypatch.setenv("ZAKURO_ENV", "staging")
    captured = _capture_post(monkeypatch, _FakeResponse(200, {"content": "x"}))
    zk.model(VALID_UUID).chat([{"role": "user", "content": "hi"}])
    assert captured["url"] == f"https://stg.api.zakuro-ai.com/api/models/{VALID_UUID}/infer"


def test_chat_hub_api_url_beats_env(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_k")
    monkeypatch.setenv("ZAKURO_ENV", "staging")
    monkeypatch.setenv("ZAKURO_API_URL", "https://custom.example/")
    captured = _capture_post(monkeypatch, _FakeResponse(200, {"content": "x"}))
    zk.model(VALID_UUID).chat([{"role": "user", "content": "hi"}])
    assert captured["url"] == f"https://custom.example/api/models/{VALID_UUID}/infer"


def test_chat_explicit_broker_still_uses_broker_infer(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_k")
    captured = _capture_post(monkeypatch, _FakeResponse(200, {"content": "x", "provider": "w"}))
    result = zk.model(VALID_UUID, broker="http://127.0.0.1:9000").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert captured["url"] == "http://127.0.0.1:9000/infer"
    assert captured["json"]["model"] == VALID_UUID
    assert result.provider == "w"


def test_chat_zakuro_broker_env_uses_broker(monkeypatch):
    monkeypatch.setenv("ZAKURO_BROKER", "http://envbroker:9000/")
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
    captured = _capture_post(monkeypatch, _FakeResponse(200, {"content": "x"}))
    zk.model(VALID_UUID).chat([{"role": "user", "content": "hi"}])
    assert captured["url"] == "http://envbroker:9000/infer"


def test_chat_hub_error_surfaces_detail_errors(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_k")
    _capture_post(
        monkeypatch,
        _FakeResponse(402, {"detail": {"errors": ["Insufficient balance", "top up"]}}),
    )
    with pytest.raises(ModelInferenceError, match=r"402.*Insufficient balance; top up"):
        zk.model(VALID_UUID).chat([{"role": "user", "content": "hi"}])


def test_chat_hub_network_error_wrapped(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "zk_k")

    def boom(*a, **k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(ModelInferenceError, match="Failed to reach hub"):
        zk.model(VALID_UUID).chat([{"role": "user", "content": "hi"}])


def test_chat_hub_without_key_names_env_var(monkeypatch):
    monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: called.append(1))
    with pytest.raises(ModelInferenceError, match="ZAKURO_API_KEY"):
        zk.model(VALID_UUID).chat([{"role": "user", "content": "hi"}])
    assert not called
