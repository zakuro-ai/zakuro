"""Where a Task's jobs go, and why that is decided locally.

A Task waits for all of its batches, so an even split across an uneven mesh
makes every Task as slow as its slowest node. Weighting fixes that — but only
if deciding the weights stays free, because the moment planning asks the mesh
anything it spends the round trips batching saved.
"""

from __future__ import annotations

import pytest

import zakuro as zk
from zakuro.mesh import NodeInfo
from zakuro.placement import OBSERVED, Observations, allocate, deal


def node(i: int) -> NodeInfo:
    return NodeInfo(
        endpoint=f"192.0.2.{i}:9000",
        node_id=f"zc://node-{i:016x}",
        name=f"n{i}",
        workers={f"w{i}"},
    )


def double(x: int) -> int:
    return x * 2


@pytest.fixture(autouse=True)
def clean_rates():
    # OBSERVED is process-level on purpose, so tests must not inherit it.
    OBSERVED.reset()
    yield
    OBSERVED.reset()


class TestAllocating:
    def test_nothing_measured_is_an_even_split(self):
        # The behaviour this replaced. Until something is observed, weighted
        # placement must be indistinguishable from round-robin.
        assert allocate(10, 3) == [4, 3, 3]
        assert allocate(10, 3, None) == [4, 3, 3]

    def test_a_faster_node_gets_proportionally_more(self):
        assert allocate(100, 3, [3, 1, 1]) == [59, 21, 20]

    def test_the_parts_always_sum_to_the_whole(self):
        for jobs, weights in ((1000, [3, 1, 1, 7, 2]), (7, [1, 5]), (3, [100, 1, 1])):
            assert sum(allocate(jobs, len(weights), weights)) == jobs

    def test_a_slow_node_still_gets_work(self):
        # The feedback trap: a node given nothing produces no measurement, and
        # a node with no measurement is never given anything again. Every node
        # keeps at least one job while there are enough to go round.
        assert allocate(3, 3, [1000.0, 0.001, 0.001]) == [1, 1, 1]
        assert min(allocate(50, 3, [1000.0, 0.001, 0.001])) >= 1

    def test_it_is_deterministic(self):
        assert allocate(97, 4, [3, 1, 4, 1]) == allocate(97, 4, [3, 1, 4, 1])

    def test_degenerate_inputs_do_not_explode(self):
        assert allocate(0, 3) == [0, 0, 0]
        assert allocate(10, 0) == []
        assert allocate(10, 3, [0, 0, 0]) == [4, 3, 3]  # all-zero is no signal
        assert allocate(10, 3, [1, 1]) == [4, 3, 3]  # mismatched length ignored


class TestDealing:
    def test_indices_are_interleaved_not_sliced(self):
        # Jobs are usually generated in order, so when cost tracks the input,
        # contiguous blocks hand one node the whole expensive tail — an uneven
        # split that no count of jobs or requests would reveal.
        assert deal(10, [4, 3, 3]) == [[0, 3, 6, 9], [1, 4, 7], [2, 5, 8]]

    def test_every_job_is_dealt_exactly_once(self):
        groups = deal(100, allocate(100, 4, [5, 1, 2, 1]))
        flat = sorted(i for g in groups for i in g)
        assert flat == list(range(100))

    def test_shares_are_honoured(self):
        assert [len(g) for g in deal(10, [5, 3, 2])] == [5, 3, 2]


class TestLearning:
    def test_an_unmeasured_node_gets_the_average_not_a_guess(self):
        # Guessing it is fast floods it; guessing it is slow starves it of the
        # work that would prove otherwise.
        o = Observations()
        o.record("a", 100, 1.0)  # 100 jobs/s
        o.record("b", 10, 1.0)  # 10 jobs/s
        assert o.weights(["a", "b", "c"]) == [100.0, 10.0, 55.0]

    def test_nothing_measured_reports_nothing_rather_than_ones(self):
        # None, not [1,1,1] — so the caller falls back to an even split
        # instead of treating "no data" as a measurement of equality.
        assert Observations().weights(["a", "b"]) is None

    def test_one_bad_batch_does_not_rewrite_a_reputation(self):
        o = Observations()
        for _ in range(5):
            o.record("a", 100, 1.0)
        before = o.rate("a")
        o.record("a", 1, 1.0)  # one terrible batch
        after = o.rate("a")
        assert after is not None and before is not None
        assert after < before, "a slow batch must register"
        assert after > before * 0.5, "but must not erase five good ones"

    def test_zero_and_negative_samples_are_ignored(self):
        o = Observations()
        o.record("a", 0, 1.0)
        o.record("a", 10, 0.0)
        o.record("a", 10, -1.0)
        assert o.rate("a") is None


class TestPlanningStaysFree:
    def test_weighted_planning_still_asks_the_mesh_nothing(self, monkeypatch):
        from zakuro import mesh

        monkeypatch.setattr(
            mesh,
            "_get_json",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("planning must not call out")),
        )
        OBSERVED.record("zc://node-0000000000000001", 100, 1.0)
        task = zk.Task()
        for i in range(100):
            task.add(double, i)
        plan = task.plan([node(1), node(2)])
        assert plan.total_jobs == 100

    def test_a_measured_mesh_favours_the_fast_node(self):
        OBSERVED.record(node(1).node_id, 100, 1.0)
        OBSERVED.record(node(2).node_id, 10, 1.0)
        task = zk.Task()
        for i in range(110):
            task.add(double, i)
        by_name = {s.node_name: s.jobs for s in task.plan([node(1), node(2)]).steps}
        assert by_name["n1"] > by_name["n2"] * 5
        assert by_name["n1"] + by_name["n2"] == 110

    def test_placement_even_pins_the_split_regardless(self):
        OBSERVED.record(node(1).node_id, 1000, 1.0)
        OBSERVED.record(node(2).node_id, 1, 1.0)
        task = zk.Task(placement="even")
        for i in range(10):
            task.add(double, i)
        assert [s.jobs for s in task.plan([node(1), node(2)]).steps] == [5, 5]

    def test_round_trips_still_track_nodes_not_jobs(self):
        # Weighting must not quietly turn one request per node into more.
        OBSERVED.record(node(1).node_id, 100, 1.0)
        task = zk.Task()
        for i in range(1000):
            task.add(double, i)
        assert task.plan([node(i) for i in range(1, 7)]).round_trips == 6
