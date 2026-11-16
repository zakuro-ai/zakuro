"""Tests for the ``zakuro-worker`` console entry point (zakuro.worker.cli)."""

from __future__ import annotations

import sys

import pytest

from zakuro.worker import cli


@pytest.fixture(autouse=True)
def _insecure_bind_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests drive argument plumbing on the default 0.0.0.0 bind.

    The fail-closed bind policy (zakuro.worker.posture.enforce_bind_policy)
    refuses that without caller auth; accept it explicitly here and test the
    refusal itself in TestBindPolicy.
    """
    for k in ("ZAKURO_AUTH_REQUIRED", "ZAKURO_WIRE", "ZAKURO_CERT_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZAKURO_INSECURE_BIND", "1")


class TestBindPolicy:
    """A routable bind with no caller authentication is refused at startup."""

    @pytest.mark.parametrize("transport", ["http", "quic"])
    def test_unauthenticated_nonloopback_bind_exits(self, monkeypatch, transport) -> None:
        import uvicorn

        from zakuro.worker import quic_server

        ran: list[str] = []
        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: ran.append("http"))
        monkeypatch.setattr(quic_server, "run_quic_worker", lambda **kw: ran.append("quic"))
        monkeypatch.delenv("ZAKURO_INSECURE_BIND")
        monkeypatch.setattr(
            sys, "argv", ["zakuro-worker", "--transport", transport, "--host", "0.0.0.0"]
        )

        with pytest.raises(SystemExit, match="refusing to bind"):
            cli.main()
        assert ran == []

    def test_loopback_bind_needs_no_override(self, monkeypatch) -> None:
        import uvicorn

        captured: dict = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(**kw))
        monkeypatch.delenv("ZAKURO_INSECURE_BIND")
        monkeypatch.setattr(sys, "argv", ["zakuro-worker", "--host", "127.0.0.1"])

        cli.main()

        assert captured["host"] == "127.0.0.1"


class TestParser:
    """Argument defaults come from the environment so containers can be configured with env only."""

    def test_defaults(self, monkeypatch) -> None:
        """With a clean environment the worker binds 0.0.0.0 over HTTP."""
        monkeypatch.delenv("ZAKURO_HOST", raising=False)
        monkeypatch.delenv("ZAKURO_TRANSPORT", raising=False)
        args = cli._build_parser().parse_args([])
        assert args.host == "0.0.0.0"
        assert args.port is None
        assert args.transport == "http"
        assert args.worker_name is None

    def test_env_supplies_host_and_transport(self, monkeypatch) -> None:
        """ZAKURO_HOST / ZAKURO_TRANSPORT are read when building the parser."""
        monkeypatch.setenv("ZAKURO_HOST", "192.0.2.7")
        monkeypatch.setenv("ZAKURO_TRANSPORT", "quic")
        args = cli._build_parser().parse_args([])
        assert args.host == "192.0.2.7"
        assert args.transport == "quic"

    def test_flags_override_env(self, monkeypatch) -> None:
        """Explicit flags win over the environment."""
        monkeypatch.setenv("ZAKURO_HOST", "192.0.2.7")
        args = cli._build_parser().parse_args(["--host", "127.0.0.1", "--port", "5000"])
        assert args.host == "127.0.0.1"
        assert args.port == 5000

    def test_unknown_transport_is_rejected(self, monkeypatch) -> None:
        """argparse constrains --transport to the two supported values."""
        monkeypatch.delenv("ZAKURO_TRANSPORT", raising=False)
        with pytest.raises(SystemExit):
            cli._build_parser().parse_args(["--transport", "carrier-pigeon"])


class TestMainHttp:
    """The default HTTP path hands off to uvicorn."""

    def test_runs_uvicorn_with_default_port(self, monkeypatch) -> None:
        """No --port and no ZAKURO_PORT means the HTTP default, 3960."""
        import uvicorn

        captured: dict = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
        monkeypatch.delenv("ZAKURO_PORT", raising=False)
        monkeypatch.delenv("ZAKURO_TRANSPORT", raising=False)
        monkeypatch.setattr(sys, "argv", ["zakuro-worker", "--host", "127.0.0.1"])

        cli.main()

        assert captured == {
            "app": "zakuro.worker.server:app",
            "host": "127.0.0.1",
            "port": 3960,
            "reload": False,
        }

    def test_port_from_env(self, monkeypatch) -> None:
        """ZAKURO_PORT is honoured when --port is absent."""
        import uvicorn

        captured: dict = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(**kw))
        monkeypatch.setenv("ZAKURO_PORT", "4242")
        monkeypatch.delenv("ZAKURO_TRANSPORT", raising=False)
        monkeypatch.setattr(sys, "argv", ["zakuro-worker"])

        cli.main()

        assert captured["port"] == 4242

    def test_worker_name_is_exported_to_the_environment(self, monkeypatch) -> None:
        """--worker-name is passed to the server process through ZAKURO_WORKER_NAME."""
        import os

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
        monkeypatch.delenv("ZAKURO_TRANSPORT", raising=False)
        monkeypatch.delenv("ZAKURO_WORKER_NAME", raising=False)
        monkeypatch.setattr(sys, "argv", ["zakuro-worker", "--worker-name", "cli-named"])

        cli.main()

        assert os.environ["ZAKURO_WORKER_NAME"] == "cli-named"

    def test_missing_uvicorn_exits_with_install_hint(self, monkeypatch) -> None:
        """Without the [worker] extra the CLI exits with an actionable message."""
        monkeypatch.delenv("ZAKURO_TRANSPORT", raising=False)
        monkeypatch.setattr(sys, "argv", ["zakuro-worker"])
        monkeypatch.setitem(sys.modules, "uvicorn", None)

        with pytest.raises(SystemExit) as excinfo:
            cli.main()

        assert "zakuro-ai[worker]" in str(excinfo.value)


class TestMainQuic:
    """--transport quic swaps uvicorn for the aioquic server."""

    def test_runs_quic_worker_with_default_port(self, monkeypatch) -> None:
        """QUIC defaults to 4433, not the HTTP port."""
        from zakuro.worker import quic_server

        captured: dict = {}
        monkeypatch.setattr(quic_server, "run_quic_worker", lambda **kw: captured.update(kw))
        monkeypatch.delenv("ZAKURO_PORT", raising=False)
        monkeypatch.setattr(sys, "argv", ["zakuro-worker", "--transport", "quic"])

        cli.main()

        assert captured == {"host": "0.0.0.0", "port": quic_server.DEFAULT_PORT}

    def test_explicit_port_wins(self, monkeypatch) -> None:
        """--port overrides both ZAKURO_PORT and the QUIC default."""
        from zakuro.worker import quic_server

        captured: dict = {}
        monkeypatch.setattr(quic_server, "run_quic_worker", lambda **kw: captured.update(kw))
        monkeypatch.setenv("ZAKURO_PORT", "9999")
        monkeypatch.setattr(sys, "argv", ["zakuro-worker", "--transport", "quic", "--port", "4500"])

        cli.main()

        assert captured["port"] == 4500

    def test_missing_aioquic_exits_with_install_hint(self, monkeypatch) -> None:
        """A checkout without the [worker] extra gets a pointer, not a traceback."""
        monkeypatch.setattr(sys, "argv", ["zakuro-worker", "--transport", "quic"])
        monkeypatch.setitem(sys.modules, "zakuro.worker.quic_server", None)

        with pytest.raises(SystemExit) as excinfo:
            cli.main()

        assert "zakuro-ai[worker]" in str(excinfo.value)
