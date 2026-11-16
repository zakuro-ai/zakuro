"""ZAK-0017: the account key is never sent over plain http to a remote host."""

from __future__ import annotations

import httpx
import pytest

from zakuro.config import Config
from zakuro.model import Model
from zakuro.security import InsecureTransportError, ensure_secure_for_key

UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_default_hub_url_is_https():
    assert Config().hub_url.startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "https://hub.zakuro-ai.com",
        "http://localhost:9000",
        "http://127.0.0.1:9000",
        "http://127.5.6.7:9000",
        "http://[::1]:9000",
        "http://10.13.13.202:3960",
    ],
)
def test_allowed_urls(url, monkeypatch):
    monkeypatch.delenv("ZAKURO_ALLOW_INSECURE_HTTP", raising=False)
    ensure_secure_for_key(url)


def test_plain_http_remote_refused(monkeypatch):
    monkeypatch.delenv("ZAKURO_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(InsecureTransportError, match="ZAKURO_ALLOW_INSECURE_HTTP"):
        ensure_secure_for_key("http://hub.example.com")


def test_opt_in_allows_plain_http(monkeypatch):
    monkeypatch.setenv("ZAKURO_ALLOW_INSECURE_HTTP", "1")
    ensure_secure_for_key("http://hub.example.com")


def _no_post(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1))
    return calls


def test_chat_key_not_sent_over_plain_http(monkeypatch):
    monkeypatch.delenv("ZAKURO_ALLOW_INSECURE_HTTP", raising=False)
    calls = _no_post(monkeypatch)
    m = Model(UUID, broker="http://broker.example.com:9000", api_key="zk_secret")
    with pytest.raises(InsecureTransportError):
        m.chat([{"role": "user", "content": "hi"}])
    assert calls == []


def test_transcribe_key_not_sent_over_plain_http(monkeypatch, tmp_path):
    monkeypatch.delenv("ZAKURO_ALLOW_INSECURE_HTTP", raising=False)
    calls = _no_post(monkeypatch)
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"x")
    m = Model(UUID, hub="http://hub.example.com", api_key="zk_secret")
    with pytest.raises(InsecureTransportError):
        m.transcribe(clip)
    assert calls == []


def test_reconcile_balance_withholds_key_over_plain_http(monkeypatch):
    from zakuro import mesh, reconcile

    monkeypatch.delenv("ZAKURO_ALLOW_INSECURE_HTTP", raising=False)
    monkeypatch.setattr(mesh, "api_url", lambda: "http://dash.example.com")
    monkeypatch.setattr(mesh, "api_key", lambda: "zk_secret")
    monkeypatch.setattr(mesh, "account_id", lambda: "u1")
    calls = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: calls.append(1))
    assert reconcile.dashboard_balance() is None
    assert calls == []
