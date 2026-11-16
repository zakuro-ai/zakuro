"""Tests for zk.Worker — the notebook-friendly CLI wrapper."""

from __future__ import annotations

import zakuro as zk


def test_spawn_and_roundtrip() -> None:
    """zk.Worker.spawn launches the CLI, serves /execute, and stops cleanly."""
    with zk.Worker.spawn(name="unit-test") as worker:
        assert worker.is_running
        assert worker.uri.startswith("zakuro://127.0.0.1:")
        info = worker.info()
        assert info["name"] == "unit-test"

        @zk.fn
        def add(a: int, b: int) -> int:
            return a + b

        assert add.to(worker.compute())(2, 3) == 5

    assert not worker.is_running


def test_compute_reachability_check_succeeds() -> None:
    """Compute(uri=...) probe should succeed against a live spawned worker."""
    with zk.Worker.spawn() as worker:
        compute = zk.Compute(uri=worker.uri, cpus=2)
        assert compute.host == "127.0.0.1"


def test_stateful_cls_roundtrip_http() -> None:
    """@cls create_instance + call_method work over the HTTP worker transport
    (RemoteProxy dispatches through the processor registry)."""
    with zk.Worker.spawn(name="cls-http") as worker:

        @zk.cls
        class Accumulator:
            def __init__(self, start: int = 0) -> None:
                self.total = start

            def add(self, x: int) -> int:
                self.total += x
                return self.total

        acc = Accumulator.to(worker.compute(verify=False))(start=100)
        assert acc.add(1) == 101
        assert acc.add(9) == 110
