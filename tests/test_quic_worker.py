"""End-to-end tests for the QUIC worker transport."""

from __future__ import annotations

import pytest

import zakuro as zk

pytestmark = pytest.mark.timeout(30)


def test_quic_worker_roundtrip() -> None:
    """zk.Worker.spawn(transport='quic') serves a cloudpickle round-trip."""
    with zk.Worker.spawn(name="quic-unit", transport="quic") as worker:
        assert worker.is_running
        assert worker.uri.startswith("quic://127.0.0.1:")
        assert worker.transport == "quic"

        @zk.fn
        def add(a: int, b: int) -> int:
            return a + b

        assert add.to(worker.compute(verify=False))(3, 4) == 7


def test_quic_worker_info() -> None:
    """INFO opcode returns the same shape the HTTP /info endpoint does."""
    with zk.Worker.spawn(name="quic-info", transport="quic") as worker:
        info = worker.info()
        assert info["name"] == "quic-info"
        assert info["transport"] == "quic"
        assert info["resources"]["cpus_total"] > 0


def test_quic_worker_exception_propagation() -> None:
    """User exceptions raised on the worker are re-raised locally."""
    with zk.Worker.spawn(transport="quic") as worker:

        @zk.fn
        def boom() -> None:
            raise ValueError("failure from QUIC worker")

        with pytest.raises(ValueError, match="failure from QUIC worker"):
            boom.to(worker.compute(verify=False))()


def test_quic_worker_many_calls_one_connection() -> None:
    """A single Compute reuses the connection across many calls."""
    with zk.Worker.spawn(transport="quic") as worker:

        @zk.fn
        def square(x: int) -> int:
            return x * x

        compute = worker.compute(verify=False)
        assert [square.to(compute)(i) for i in range(10)] == [i * i for i in range(10)]


def test_quic_execute_retries_on_transient_connection_error() -> None:
    """When the in-flight request fails with ConnectionError, the processor
    should tear down + reconnect + retry once before propagating."""
    from unittest.mock import MagicMock, patch

    from zakuro.processors.base import ProcessorConfig
    from zakuro.processors.quic import QuicProcessor

    # A throwaway QuicProcessor to manipulate directly — we mock the run-sync
    # boundary so we don't actually need a live worker.
    compute = zk.Compute(uri="quic://127.0.0.1:4455", verify=False)
    config = ProcessorConfig(scheme="quic", host="127.0.0.1", port=4455)
    proc = QuicProcessor(config, compute)
    proc._connected = True
    proc._protocol = MagicMock()

    import cloudpickle

    # First call: raise ConnectionError. Second call: succeed with status=OK
    # and a cloudpickle-of-42 payload.
    responses = iter(
        [
            ConnectionError("reset"),
            (0, cloudpickle.dumps(42)),
        ]
    )

    def fake_run_sync(_coro, timeout=None):
        out = next(responses)
        if isinstance(out, Exception):
            raise out
        return out

    func_bytes = cloudpickle.dumps(lambda x: x)
    args = (42,)
    kwargs = {}

    with (
        patch("zakuro.processors.quic._run_sync", side_effect=fake_run_sync),
        patch.object(proc, "_reconnect", return_value=True),
    ):
        result = proc.execute(func_bytes, args, kwargs)
    # The bytes returned by fake_run_sync second call are a cloudpickle of 42.
    assert result == 42
