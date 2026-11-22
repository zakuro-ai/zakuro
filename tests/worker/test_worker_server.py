"""Tests for the FastAPI worker app in zakuro.worker.server.

The spawned-worker tests in test_worker_runner.py drive the server through a
subprocess; these drive the ASGI app in-process so the handlers themselves are
exercised and asserted on.
"""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Iterator

import cloudpickle
import pytest
from fastapi.testclient import TestClient

from zakuro import __version__
from zakuro.worker import server


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient whose context triggers the startup/shutdown hooks."""
    with TestClient(server.app) as test_client:
        yield test_client


def test_health_reports_healthy(client: TestClient) -> None:
    """/health (probed by Compute verification) aliases /ready: 200 when ready."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_root_advertises_service_and_version(client: TestClient) -> None:
    """/ is the human landing page and reports the installed SDK version."""
    assert client.get("/").json() == {
        "service": "Zakuro Worker",
        "version": __version__,
        "docs": "/docs",
    }


def test_startup_creates_thread_pool_and_shutdown_disposes_it() -> None:
    """The startup hook installs the executor; shutdown tears it down."""
    with TestClient(server.app):
        assert server.executor is not None
        pool = server.executor
    # shutdown() drains the pool; submitting afterwards is rejected.
    with pytest.raises(RuntimeError):
        pool.submit(int)


def test_info_reports_name_from_environment(client: TestClient, monkeypatch) -> None:
    """ZAKURO_WORKER_NAME / _TYPE / pricing env vars flow into /info verbatim."""
    monkeypatch.setenv("ZAKURO_WORKER_NAME", "unit-info")
    monkeypatch.setenv("ZAKURO_WORKER_TYPE", "gpu")
    monkeypatch.setenv("ZAKURO_PRICE_PER_HOUR", "1.25")
    monkeypatch.setenv("ZAKURO_MIN_CHARGE", "0.5")
    monkeypatch.setenv("ZAKURO_WORKER_TAGS", "eu,spot")

    info = client.get("/info").json()

    assert info["name"] == "unit-info"
    assert info["worker_type"] == "gpu"
    assert info["version"] == __version__
    assert info["pricing"] == {"price_per_hour": 1.25, "min_charge": 0.5}
    assert info["tags"] == ["eu", "spot"]
    assert info["resources"]["cpus_total"] >= 1.0
    assert info["resources"]["memory_total"] > 0


def test_info_defaults_when_env_is_empty(client: TestClient, monkeypatch) -> None:
    """Unset env gives a hostname-derived name, the zakuro type, and no tags."""
    for var in (
        "ZAKURO_WORKER_NAME",
        "ZAKURO_WORKER_TYPE",
        "ZAKURO_PRICE_PER_HOUR",
        "ZAKURO_MIN_CHARGE",
        "ZAKURO_WORKER_TAGS",
    ):
        monkeypatch.delenv(var, raising=False)

    info = client.get("/info").json()

    assert info["name"].startswith("worker-")
    assert info["worker_type"] == "zakuro"
    assert info["tags"] == []
    assert info["pricing"]["price_per_hour"] == 540.0


def test_info_reports_gpus_when_nvidia_smi_answers(client: TestClient, monkeypatch) -> None:
    """A successful nvidia-smi query is parsed into gpu count, model and VRAM."""

    class _Completed:
        returncode = 0
        stdout = "NVIDIA H100 80GB HBM3, 81559\nNVIDIA H100 80GB HBM3, 81559\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())

    info = client.get("/info").json()

    assert info["resources"]["gpus_total"] == 2
    assert info["resources"]["gpus_available"] == 2
    assert info["hardware"]["gpu_model"] == "NVIDIA H100 80GB HBM3"
    assert info["hardware"]["gpu_vram_gb"] == 79


def test_info_reports_no_gpus_when_nvidia_smi_is_missing(client: TestClient, monkeypatch) -> None:
    """No nvidia-smi on PATH must degrade to zero GPUs, not blow up /info."""

    def _missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", _missing)

    info = client.get("/info").json()

    assert info["resources"]["gpus_total"] == 0
    assert info["hardware"]["gpu_model"] is None
    assert info["hardware"]["gpu_vram_gb"] is None


def test_execute_runs_the_function_and_returns_pickled_result(client: TestClient) -> None:
    """/execute is the hot path: cloudpickle in, cloudpickle out."""
    payload = cloudpickle.dumps({"func": lambda a, b: a * b, "args": (6, 7), "kwargs": {}})

    response = client.post("/execute", content=payload)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert cloudpickle.loads(response.content) == 42


def test_execute_returns_user_exception_in_body_with_status_200(client: TestClient) -> None:
    """A failing user function is a 200 carrying the pickled exception."""

    def explode() -> None:
        raise RuntimeError("user code failed")

    response = client.post("/execute", content=cloudpickle.dumps({"func": explode}))

    assert response.status_code == 200
    result = cloudpickle.loads(response.content)
    assert isinstance(result, RuntimeError)
    assert str(result) == "user code failed"


def test_execute_returns_503_before_startup(monkeypatch) -> None:
    """Without a thread pool the worker is not ready and says so."""
    monkeypatch.setattr(server, "executor", None)
    # No `with`, so the startup hook never runs and the pool stays None.
    client = TestClient(server.app)
    response = client.post("/execute", content=cloudpickle.dumps({"func": lambda: 1}))
    assert response.status_code == 503
    assert response.json()["detail"] == "Worker not ready"


def test_execute_serializes_transport_level_failures(client: TestClient, monkeypatch) -> None:
    """If dispatch itself fails, the error is pickled back rather than 500-ing."""

    def _broken(payload: bytes) -> bytes:
        raise OSError("thread pool exploded")

    monkeypatch.setattr(server, "execute_function", _broken)

    response = client.post("/execute", content=cloudpickle.dumps({"func": lambda: 1}))

    assert response.status_code == 200
    assert isinstance(cloudpickle.loads(response.content), OSError)


def test_main_forwards_host_port_and_worker_name_to_uvicorn(monkeypatch) -> None:
    """`python -m zakuro.worker.server` wiring: CLI args reach uvicorn.run."""
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs))
    monkeypatch.setattr(
        "sys.argv",
        ["zakuro-worker", "--host", "127.0.0.1", "--port", "4100", "--worker-name", "named"],
    )
    monkeypatch.delenv("ZAKURO_WORKER_NAME", raising=False)
    monkeypatch.delenv("ZAKURO_ACCESS_LOG", raising=False)

    server.main()

    assert captured == {
        "app": "zakuro.worker.server:app",
        "host": "127.0.0.1",
        "port": 4100,
        "reload": False,
        "access_log": False,
    }
    import os

    assert os.environ["ZAKURO_WORKER_NAME"] == "named"


class TestReportedRuntime:
    """`/info` states which interpreter will run the job (zc#327).

    Every other field it reports is capacity -- CPUs, RAM, GPUs -- where a
    shortfall means slow or queued. This one is capability: a cloudpickled
    function carries raw CPython bytecode, opcode numbers are renumbered
    between minor versions (LOAD_FAST is 124 up to 3.12 and 85 from 3.13), and
    a worker on the wrong one fails EVERY request while looking healthy.
    Without this in `/info` a broker cannot know that before routing there.
    """

    @pytest.mark.asyncio
    async def test_info_names_the_interpreter(self):
        from zakuro.worker.server import info

        reported = await info()
        assert reported["python_version"] == platform.python_version()
        assert reported["python_implementation"] == platform.python_implementation()

    @pytest.mark.asyncio
    async def test_the_version_is_a_real_triple(self):
        # A broker compares these, so "3.12.3" must not arrive as "3.12" or as
        # the whole sys.version banner.
        from zakuro.worker.server import info

        parts = (await info())["python_version"].split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    @pytest.mark.asyncio
    async def test_it_sits_beside_version_not_under_hardware(self):
        # The interpreter is not hardware. zc reads it from the top level.
        from zakuro.worker.server import info

        reported = await info()
        assert "python_version" in reported
        assert "python_version" not in reported["hardware"]


def test_execute_rejects_declared_oversize_with_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-Length over the cap is refused before the body is read (#36)."""
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")
    response = client.post("/execute", content=b"x" * 64)
    assert response.status_code == 413


def test_execute_rejects_chunked_oversize_with_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body with no Content-Length is counted while streaming (#36)."""
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")

    def gen() -> Iterator[bytes]:
        yield b"x" * 10
        yield b"x" * 10

    response = client.post("/execute", content=gen())
    assert response.status_code == 413


def test_execute_accepts_body_within_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "1048576")
    payload = cloudpickle.dumps({"func": lambda: 7, "args": (), "kwargs": {}})
    response = client.post("/execute", content=payload)
    assert response.status_code == 200
    assert cloudpickle.loads(response.content) == 7


def test_execute_does_not_go_through_fastapi_routing(client: TestClient, monkeypatch) -> None:
    """POST /execute is served past FastAPI's routing and dependency solving.

    For a one-endpoint job server the framework cost the worker several times
    the job: a py-spy profile under load put ~60 % of the worker's CPU in the
    FastAPI/Starlette stack and ~16 % in running the function (2026-11-21).
    """
    import fastapi.routing

    calls = {"n": 0}
    real = fastapi.routing.solve_dependencies

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "solve_dependencies", counting)
    assert client.get("/health").status_code == 200
    assert calls["n"] >= 1, "the counter sees a routed request"
    calls["n"] = 0
    payload = cloudpickle.dumps({"func": lambda x: x * 2, "args": (21,), "kwargs": {}})
    response = client.post("/execute", content=payload)
    assert response.status_code == 200
    assert cloudpickle.loads(response.content) == 42
    assert calls["n"] == 0, "/execute went through FastAPI's routing"


def test_the_access_log_is_off_unless_asked(monkeypatch) -> None:
    """One log line per job is the worker's busiest output and ~4 % of its CPU
    under load; ZAKURO_ACCESS_LOG=1 turns it back on."""
    import uvicorn

    from zakuro.worker import cli

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    monkeypatch.setattr("sys.argv", ["zakuro-worker", "--host", "127.0.0.1"])
    monkeypatch.delenv("ZAKURO_ACCESS_LOG", raising=False)
    cli.main()
    assert captured["access_log"] is False
    monkeypatch.setenv("ZAKURO_ACCESS_LOG", "1")
    cli.main()
    assert captured["access_log"] is True
