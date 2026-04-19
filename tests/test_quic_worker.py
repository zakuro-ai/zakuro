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
