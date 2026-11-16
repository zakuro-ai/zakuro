"""A Task: planned once, dispatched in as few trips as possible.

The property worth defending is the SHAPE of the traffic. On a mesh the round
trip is the cost -- a worker answers in about a millisecond while reaching it
takes hundreds -- so the number that matters is requests per NODE, never
requests per job. Measured on a live seven-node mesh: 60 jobs took 167s as
individual calls and 21.8s as one Task, 60 round trips against 7.

Planning must not itself be chatty, or it only moves the overhead. These pin
that a plan asks the mesh nothing.
"""

from __future__ import annotations

import struct

import cloudpickle
import pytest

import zakuro as zk
from zakuro.mesh import NodeInfo
from zakuro.task import _decode_results, _encode_request


def node(i: int, workers: bool = True) -> NodeInfo:
    return NodeInfo(
        endpoint=f"192.0.2.{i}:9000",
        node_id=f"zc://node-{i:016x}",
        name=f"n{i}",
        workers={f"w{i}"} if workers else set(),
    )


def double(x: int) -> int:
    return x * 2


class TestTheWireFormat:
    """`ZKB1` has to match zc's Rust codec byte for byte, or nothing lands."""

    def test_a_frame_is_magic_count_then_jobs(self):
        frame = _encode_request([(7, b"", b"payload")])
        assert frame[:4] == b"ZKB1"
        assert struct.unpack_from("<I", frame, 4)[0] == 1
        assert struct.unpack_from("<I", frame, 8)[0] == 7  # job id
        assert struct.unpack_from("<I", frame, 12)[0] == 0  # requirements len
        assert struct.unpack_from("<I", frame, 16)[0] == len(b"payload")
        assert frame[20:] == b"payload"

    def test_per_job_requirements_ride_along(self):
        frame = _encode_request([(1, b"{}", b"p")])
        assert struct.unpack_from("<I", frame, 12)[0] == 2
        assert frame[16:18] == b"{}"

    def test_results_are_matched_by_id_not_by_order(self):
        # zc streams results as they FINISH, so a batch comes back in
        # completion order. Matching positionally would mis-assign every value
        # in a batch whose jobs take different times -- silently.
        body = b""
        for job_id, status, payload in ((2, 0, b"two"), (0, 0, b"zero"), (1, 1, b"boom")):
            body += struct.pack("<I", job_id) + bytes([status])
            body += struct.pack("<I", len(payload)) + payload
        assert _decode_results(body) == [
            (2, True, b"two"),
            (0, True, b"zero"),
            (1, False, b"boom"),
        ]

    def test_a_partial_trailing_frame_is_not_an_error(self):
        # A streamed reply arrives in pieces; a short tail means more is
        # coming, not that the stream is corrupt.
        good = struct.pack("<I", 0) + bytes([0]) + struct.pack("<I", 2) + b"ok"
        assert _decode_results(good + b"\x01\x02") == [(0, True, b"ok")]

    def test_an_unknown_status_byte_is_refused(self):
        bad = struct.pack("<I", 0) + bytes([9]) + struct.pack("<I", 0)
        with pytest.raises(ValueError, match="status"):
            _decode_results(bad)

    def test_a_frame_beyond_zcs_caps_fails_here_naming_the_job(self):
        # zc rejects these on the far side; failing locally says which job.
        with pytest.raises(ValueError, match="at most"):
            _encode_request([(i, b"", b"x") for i in range(10_001)])
        with pytest.raises(ValueError, match="job 3"):
            _encode_request([(3, b"", b"x" * (64 * 1024 * 1024 + 1))])


class TestPlanningIsFree:
    """A plan is computed from the cached node view and asks the mesh nothing."""

    def test_planning_makes_no_network_calls(self, monkeypatch):
        from zakuro import mesh

        def explode(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("planning must not talk to the mesh")

        monkeypatch.setattr(mesh, "_get_json", explode)
        task = zk.Task()
        for i in range(500):
            task.add(double, i)
        plan = task.plan([node(i) for i in range(5)])
        assert plan.total_jobs == 500

    def test_round_trips_track_nodes_not_jobs(self):
        # The whole point. 1000 jobs over 6 nodes is 6 requests.
        task = zk.Task()
        for i in range(1000):
            task.add(double, i)
        plan = task.plan([node(i) for i in range(6)])
        assert plan.round_trips == 6
        assert sum(s.jobs for s in plan.steps) == 1000

    def test_a_batch_larger_than_zcs_cap_becomes_more_frames_not_a_failure(self):
        task = zk.Task()
        for i in range(25_000):
            task.add(double, i)
        plan = task.plan([node(0)])
        # 25k jobs on one node: three frames of at most 10k.
        assert plan.round_trips == 3

    def test_a_node_with_no_workers_is_skipped_with_a_reason(self):
        # On a mesh the interesting question is usually why something was NOT
        # used. Three such nodes were live on the measured mesh.
        task = zk.Task().add(double, 1)
        plan = task.plan([node(1), node(2, workers=False)])
        assert [s.node_name for s in plan.steps] == ["n1"]
        assert plan.skipped == [("n2", "no workers of its own")]

    def test_the_split_is_even_and_deterministic(self):
        task = zk.Task()
        for i in range(10):
            task.add(double, i)
        first = task.plan([node(i) for i in range(3)])
        second = zk.Task()
        for i in range(10):
            second.add(double, i)
        assert [s.job_indices for s in first.steps] == [
            s.job_indices for s in second.plan([node(i) for i in range(3)]).steps
        ]
        assert sorted(j for s in first.steps for j in s.job_indices) == list(range(10))


class TestRunning:
    def test_running_with_nowhere_to_run_says_why(self):
        task = zk.Task().add(double, 1)
        with pytest.raises(RuntimeError) as e:
            task.run([node(2, workers=False)])
        assert "no workers of its own" in str(e.value)

    def test_a_job_whose_frame_never_returns_is_a_failure_not_a_none(self, monkeypatch):
        # Silence is the dangerous case: a missing result must not read as a
        # successful None.
        task = zk.Task()
        for i in range(3):
            task.add(double, i)
        monkeypatch.setattr(task, "_dispatch", lambda *a, **k: None)
        results = task.run([node(0)])
        assert [r.ok for r in results] == [False, False, False]
        assert "no result frame" in results[0].error

    def test_a_payload_round_trips_through_cloudpickle(self):
        task = zk.Task().add(double, 21)
        sent = cloudpickle.loads(task.jobs[0].payload())
        assert sent["args"] == (21,)
        assert sent["func"](21) == 42
