"""Tests for zakuro.client.ZakuroClient — the HTTP transport to a worker."""

from __future__ import annotations

import httpx
import pytest

from zakuro import Compute
from zakuro.client import ZakuroClient
from zakuro.config import Config


def _client(handler, *, config: Config | None = None) -> ZakuroClient:
    """A ZakuroClient whose httpx client is pinned to a mock transport."""
    compute = Compute(host="worker", port=3960, verify=False)
    client = ZakuroClient(compute, config=config or Config(auth_token=None))
    client._client = httpx.Client(base_url=compute.endpoint, transport=httpx.MockTransport(handler))
    return client


class TestConfigAndAuth:
    """Configuration is lazy, and the auth token becomes a Bearer header."""

    def test_config_is_loaded_lazily(self, monkeypatch) -> None:
        """A client built without a Config resolves one on first access."""
        monkeypatch.setattr(Config, "load", staticmethod(lambda: Config(auth_token="from-disk")))
        client = ZakuroClient(Compute(host="worker", verify=False))
        assert client.config.auth_token == "from-disk"

    def test_auth_token_becomes_bearer_header(self) -> None:
        """A configured token is attached to every worker request."""
        client = ZakuroClient(Compute(host="worker", verify=False), config=Config(auth_token="tok"))
        assert client._auth_headers() == {"Authorization": "Bearer tok"}

    def test_no_token_means_no_header(self, monkeypatch, tmp_path) -> None:
        """An unauthenticated worker gets no Authorization header at all.

        `ZAKURO_HOME` is isolated because "no token" now means no configured
        token AND no credentials file: the client falls back to the one
        `zc login` writes, so without this the test passes on a logged-out
        machine and fails on a logged-in one.
        """
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
        monkeypatch.delenv("ZAKURO_AUTH", raising=False)
        client = ZakuroClient(Compute(host="worker", verify=False), config=Config())
        assert client._auth_headers() == {}

    def test_the_credentials_file_is_the_fallback(self, monkeypatch, tmp_path) -> None:
        """With no configured token, `zc login`'s key is used.

        This is what lets a remote class work from a plain shell, for the same
        reason `@zk.fn` does.
        """
        (tmp_path / "credentials").write_text("api_key: zk_from_file\n")
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        monkeypatch.delenv("ZAKURO_API_KEY", raising=False)
        monkeypatch.delenv("ZAKURO_AUTH", raising=False)
        client = ZakuroClient(Compute(host="worker", verify=False), config=Config())
        assert client._auth_headers() == {"Authorization": "Bearer zk_from_file"}

    def test_a_configured_token_wins_over_the_file(self, monkeypatch, tmp_path) -> None:
        (tmp_path / "credentials").write_text("api_key: zk_from_file\n")
        monkeypatch.setenv("ZAKURO_HOME", str(tmp_path))
        client = ZakuroClient(
            Compute(host="worker", verify=False), config=Config(auth_token="explicit")
        )
        assert client._auth_headers() == {"Authorization": "Bearer explicit"}

    def test_client_is_built_once_and_reused(self) -> None:
        """The httpx client is pooled across calls, not rebuilt per request."""
        client = ZakuroClient(Compute(host="worker", verify=False), config=Config())
        assert client.client is client.client
        client.close()


class TestExecute:
    """execute() posts the cloudpickle payload to /execute."""

    def test_posts_payload_with_octet_stream_content_type(self) -> None:
        """The worker expects a binary body; the content type says so."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["content_type"] = request.headers["Content-Type"]
            seen["body"] = request.content
            return httpx.Response(200, content=b"result-bytes")

        assert _client(handler).execute(b"payload") == b"result-bytes"
        assert seen["url"] == "http://worker:3960/execute"
        assert seen["content_type"] == "application/octet-stream"
        assert seen["body"] == b"payload"

    def test_extra_headers_are_merged(self) -> None:
        """Instance-affinity headers from @cls proxies ride along."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["action"] = request.headers["X-Zakuro-Instance-Action"]
            seen["content_type"] = request.headers["Content-Type"]
            return httpx.Response(200, content=b"ok")

        _client(handler).execute(b"p", extra_headers={"X-Zakuro-Instance-Action": "call_method"})
        assert seen["action"] == "call_method"
        assert seen["content_type"] == "application/octet-stream"

    def test_error_status_raises(self) -> None:
        """A worker-level HTTP error is not silently swallowed."""
        with pytest.raises(httpx.HTTPStatusError):
            _client(lambda request: httpx.Response(500)).execute(b"p")


class TestPingAndInfo:
    """ping() never raises; info() does on a bad status."""

    def test_ping_true_on_200(self) -> None:
        """A healthy worker answers 200 on /health."""
        assert _client(lambda request: httpx.Response(200)).ping() is True

    def test_ping_false_on_error_status(self) -> None:
        """Any non-200 is reported as unhealthy."""
        assert _client(lambda request: httpx.Response(503)).ping() is False

    def test_ping_false_when_unreachable(self) -> None:
        """A transport error means unhealthy, not an exception to the caller."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        assert _client(handler).ping() is False

    def test_info_returns_parsed_json(self) -> None:
        """/info is the worker capability document."""
        client = _client(lambda request: httpx.Response(200, json={"name": "w1", "gpus": 0}))
        assert client.info() == {"name": "w1", "gpus": 0}

    def test_info_raises_on_error_status(self) -> None:
        """An unhealthy worker must not yield a half-parsed info dict."""
        with pytest.raises(httpx.HTTPStatusError):
            _client(lambda request: httpx.Response(404)).info()


class TestLifecycle:
    """close()/context manager/repr."""

    def test_close_is_idempotent(self) -> None:
        """Closing twice is safe; the pooled client is dropped."""
        client = _client(lambda request: httpx.Response(200))
        client.close()
        assert client._client is None
        client.close()

    def test_context_manager_closes_on_exit(self) -> None:
        """`with ZakuroClient(...)` releases the connection pool."""
        client = _client(lambda request: httpx.Response(200))
        with client as entered:
            assert entered is client
        assert client._client is None

    def test_repr_shows_the_endpoint(self) -> None:
        """repr is used in error messages, so it must name the worker."""
        client = ZakuroClient(Compute(host="worker", port=3960, verify=False))
        assert repr(client) == "ZakuroClient(endpoint='http://worker:3960')"
