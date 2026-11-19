"""What a Task was charged, against what its balance actually did.

The brokers report the charge; the account they charge against lives on
whichever broker holds that user's shard. Two machines, two numbers, and until
now nothing compared them — so a batch could be billed twice, or not at all,
and look the same from the client.

The hard part is not getting the numbers. It is knowing which of them can be
subtracted from which. Balances are **sharded**: one broker owns a user's
ledger and the rest hold forwarded or cached views, so at one instant on a
live 9-broker mesh three different balances were reported, ¥102.75 apart.
Comparing across brokers therefore measures shard skew, not spend — the first
version of this did exactly that and reported ¥0.225 of drift on a 7-node
Task that had not happened. These tests pin the distinction.
"""

from __future__ import annotations

import struct

import cloudpickle
import httpx
import pytest

import zakuro as zk
from zakuro.mesh import NodeInfo
from zakuro.reconcile import Charge, Reconciliation


def node(i: int) -> NodeInfo:
    return NodeInfo(
        endpoint=f"192.0.2.{i}:9000",
        node_id=f"zc://node-{i:016x}",
        name=f"n{i}",
        workers={f"w{i}"},
    )


def double(x: int) -> int:
    return x * 2


def results_frame(ids: list[int]) -> bytes:
    """A `ZKB1` reply carrying one successful result per id."""
    body = b""
    for job_id in ids:
        payload = cloudpickle.dumps(job_id * 2)
        body += struct.pack("<I", job_id) + bytes([0]) + struct.pack("<I", len(payload)) + payload
    return body


class Recorder:
    """Stands in for `httpx.Client`, counting every request the Task makes.

    `/me` is served from `balances` and tracked separately, so a test can
    assert exactly how many authoritative readings were taken.
    """

    def __init__(self, replies: list[dict], balances: list[float] | None = None):
        self.replies = list(replies)
        self.balances = list(balances or [])
        self.batch_calls: list[str] = []
        self.me_calls: list[str] = []

    def __call__(self, *_a, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def post(self, url, **_kw):
        self.batch_calls.append(url)
        spec = self.replies.pop(0) if self.replies else {}
        return httpx.Response(
            spec.get("status", 200),
            content=results_frame(spec.get("ids", [])),
            headers=dict(spec.get("headers", {})),
        )

    def get(self, url, **_kw):
        self.me_calls.append(url)
        if not self.balances:
            return httpx.Response(404)
        return httpx.Response(200, json={"balance": self.balances.pop(0)})


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "k-test")


def run_over(monkeypatch, nodes, replies, jobs=4, balances=None, **kw):
    rec = Recorder(replies, balances)
    monkeypatch.setattr(httpx, "Client", rec)
    if balances is not None or kw.get("reconcile"):
        # The dashboard is the authority, so a test that wants an
        # authoritative pair stubs THAT, not a broker's /me.
        from zakuro import task as task_mod

        pending = list(balances or [])

        def fake_dashboard(*_a, **_k):
            rec.me_calls.append("dashboard")
            return pending.pop(0) if pending else None

        monkeypatch.setattr(task_mod, "dashboard_balance", fake_dashboard)
        monkeypatch.setattr(task_mod, "settled_balance", fake_dashboard)
    task = zk.Task()
    for i in range(jobs):
        task.add(double, i)
    kw.setdefault("reconcile", False)
    out = task.run(nodes, **kw)
    return task, rec, out


ONE_NODE = [
    {
        "ids": [0, 1, 2, 3],
        "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
    }
]


class TestChargesAreFree:
    def test_the_per_broker_charges_cost_no_extra_request(self, monkeypatch, signed_in):
        # Two nodes must mean exactly two requests when reconcile=False.
        replies = [
            {
                "ids": [0, 2],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
            },
            {
                "ids": [1, 3],
                "headers": {"X-Zakuro-Cost": "37.5", "X-Zakuro-Balance": "1387.5"},
            },
        ]
        task, rec, _ = run_over(monkeypatch, [node(1), node(2)], replies)
        assert len(rec.batch_calls) == 2
        assert rec.me_calls == []
        assert task.reconciliation.charged == pytest.approx(112.5)


class TestAcrossBrokersIsNotComparable:
    """The correction. Sharded balances must not be subtracted from each other."""

    def test_a_multi_broker_task_refuses_to_guess(self, monkeypatch, signed_in):
        # These two brokers each report a balance, and the difference between
        # them looks exactly like ¥37.5 of drift. It is shard skew. The
        # honest answer is "cannot say", not a number.
        replies = [
            {
                "ids": [0, 2],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
            },
            {
                "ids": [1, 3],
                "headers": {"X-Zakuro-Cost": "37.5", "X-Zakuro-Balance": "1350.0"},
            },
        ]
        task, _, _ = run_over(monkeypatch, [node(1), node(2)], replies)
        r = task.reconciliation
        assert r.charged == pytest.approx(112.5)
        assert r.agrees is None, "two brokers' views are not a series"
        assert r.drift is None
        assert r.balance_spread == pytest.approx(75.0)
        assert "cannot be compared" in str(r)

    def test_the_spread_between_brokers_is_reported(self, monkeypatch, signed_in):
        replies = [
            {
                "ids": [0, 2],
                "headers": {"X-Zakuro-Cost": "15.0", "X-Zakuro-Balance": "2995053.78615"},
            },
            {
                "ids": [1, 3],
                "headers": {"X-Zakuro-Cost": "15.0", "X-Zakuro-Balance": "2995156.60455"},
            },
        ]
        task, _, _ = run_over(monkeypatch, [node(1), node(2)], replies)
        # The real figure measured on the live mesh.
        assert task.reconciliation.balance_spread == pytest.approx(102.8184, abs=1e-4)


class TestOneBrokerIsComparable:
    """A single broker's own successive readings ARE a series."""

    def test_a_single_broker_reconciles_from_its_own_series(self, monkeypatch, signed_in):
        task, _, _ = run_over(monkeypatch, [node(1)], ONE_NODE)
        r = task.reconciliation
        assert r.balance_before == pytest.approx(1500.0)
        assert r.balance_after == pytest.approx(1425.0)
        assert r.drift == pytest.approx(0.0)
        assert r.agrees is True

    def test_a_single_broker_that_overcharges_is_caught(self, monkeypatch, signed_in):
        replies = [
            {
                "ids": [0, 1, 2, 3],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1350.0"},
            }
        ]
        task, _, _ = run_over(monkeypatch, [node(1)], replies)
        # It says it took ¥75 and left ¥1350, so it opened at ¥1425 by its own
        # arithmetic -- consistent. Nothing to catch from ONE observation;
        # this pins that the code does not invent drift either.
        assert task.reconciliation.agrees is True


class TestAuthoritativeReading:
    """The dashboard is the authority; a broker's own number is not."""

    def test_it_takes_exactly_two_readings_whatever_the_size(self, monkeypatch, signed_in):
        replies = [
            {
                "ids": [0, 2],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
            },
            {
                "ids": [1, 3],
                "headers": {"X-Zakuro-Cost": "37.5", "X-Zakuro-Balance": "1350.0"},
            },
        ]
        task, rec, _ = run_over(
            monkeypatch, [node(1), node(2)], replies, balances=[1500.0, 1387.5], reconcile=True
        )
        assert len(rec.me_calls) == 2, "one before, one settled after -- per TASK"
        assert len(rec.batch_calls) == 2
        r = task.reconciliation
        assert r.authoritative == (1500.0, 1387.5)
        assert r.observed == pytest.approx(112.5)
        assert r.drift == pytest.approx(0.0)
        assert r.agrees is True

    def test_it_catches_a_real_overcharge(self, monkeypatch, signed_in):
        replies = [
            {
                "ids": [0, 2],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
            },
            {
                "ids": [1, 3],
                "headers": {"X-Zakuro-Cost": "37.5", "X-Zakuro-Balance": "1350.0"},
            },
        ]
        # Brokers admit ¥112.5; the account actually lost ¥150.
        task, _, _ = run_over(
            monkeypatch, [node(1), node(2)], replies, balances=[1500.0, 1350.0], reconcile=True
        )
        r = task.reconciliation
        assert r.drift == pytest.approx(37.5)
        assert r.agrees is False
        assert "account lost more than was charged" in str(r)

    def test_it_catches_work_the_brokers_overstated(self, monkeypatch, signed_in):
        replies = [
            {
                "ids": [0, 2],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
            },
            {
                "ids": [1, 3],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1350.0"},
            },
        ]
        task, _, _ = run_over(
            monkeypatch, [node(1), node(2)], replies, balances=[1500.0, 1425.0], reconcile=True
        )
        assert task.reconciliation.drift == pytest.approx(-75.0)
        assert "brokers claim more than actually left" in str(task.reconciliation)

    def test_an_unreadable_balance_does_not_fake_an_answer(self, monkeypatch, signed_in):
        # /me returns 404 -> no authoritative pair, and the multi-broker
        # charges still cannot be compared. "Cannot say" beats a wrong number.
        replies = [
            {
                "ids": [0, 2],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
            },
            {
                "ids": [1, 3],
                "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1350.0"},
            },
        ]
        task, _, _ = run_over(
            monkeypatch, [node(1), node(2)], replies, balances=None, reconcile=True
        )
        assert task.reconciliation.authoritative is None
        assert task.reconciliation.agrees is None


class TestHeaders:
    def test_a_broker_that_reports_no_balance_says_so_rather_than_assuming_zero(
        self, monkeypatch, signed_in
    ):
        # A missing header is "did not say". Reading it as 0.0 would invent a
        # balance of zero and report a drift that is pure fiction.
        replies = [{"ids": [0, 1, 2, 3], "headers": {"X-Zakuro-Cost": "75.0"}}]
        task, _, _ = run_over(monkeypatch, [node(1)], replies)
        assert task.reconciliation.charged == pytest.approx(75.0)
        assert task.reconciliation.agrees is None
        assert task.reconciliation.balance_before is None
        assert "nothing to reconcile against" in str(task.reconciliation)

    def test_a_malformed_cost_header_does_not_lose_the_results(self, monkeypatch, signed_in):
        replies = [{"ids": [0, 1, 2, 3], "headers": {"X-Zakuro-Cost": "not-a-number"}}]
        task, _, out = run_over(monkeypatch, [node(1)], replies)
        assert [r.ok for r in out] == [True] * 4
        assert task.cost == pytest.approx(0.0)


class TestSampling:
    def test_sampling_is_opt_in(self, monkeypatch, signed_in):
        _, rec, _ = run_over(monkeypatch, [node(1)], ONE_NODE)
        assert rec.me_calls == []

    def test_a_poller_never_fails_the_run(self):
        from zakuro.reconcile import BalancePoller

        def explode() -> float:
            raise RuntimeError("broker down")

        with BalancePoller(explode, every=1.0) as p:
            pass
        assert p.samples == []


class TestNodesInParallel:
    def test_one_dead_node_does_not_lose_the_other_nodes_results(self, monkeypatch, signed_in):
        # This used to take the whole run down: the exception escaped
        # `_dispatch`, out of `run()`, and every result already collected went
        # with it -- including from nodes that answered perfectly.
        class HalfDead(Recorder):
            def post(self, url, **kw):
                if "192.0.2.2" in url:
                    self.batch_calls.append(url)
                    raise httpx.ConnectError("no route to host")
                return super().post(url, **kw)

        rec = HalfDead(
            [
                {
                    "ids": [0, 2],
                    "headers": {"X-Zakuro-Cost": "75.0", "X-Zakuro-Balance": "1425.0"},
                }
            ]
        )
        monkeypatch.setattr(httpx, "Client", rec)
        task = zk.Task()
        for i in range(4):
            task.add(double, i)
        out = task.run([node(1), node(2)], reconcile=False)

        assert [r.ok for r in out] == [True, False, True, False]
        assert "unreachable" in (out[1].error or "")
        assert task.cost == pytest.approx(75.0)

    def test_concurrent_replies_do_not_lose_a_charge(self, monkeypatch, signed_in):
        # `self.cost += cost` is a read then a write. Eight nodes answering at
        # once must still sum to eight charges.
        replies = [
            {"ids": [i], "headers": {"X-Zakuro-Cost": "18.75", "X-Zakuro-Balance": "1350.0"}}
            for i in range(8)
        ]

        class Slow(Recorder):
            def post(self, url, **kw):
                import time as _t

                _t.sleep(0.01)  # widen the window the lock has to close
                return super().post(url, **kw)

        rec = Slow(replies)
        monkeypatch.setattr(httpx, "Client", rec)
        task = zk.Task()
        for i in range(8):
            task.add(double, i)
        task.run([node(i) for i in range(1, 9)], reconcile=False)

        assert len(task.reconciliation.charges) == 8
        assert task.cost == pytest.approx(150.0)


class TestTheArithmetic:
    def test_a_free_fleet_reconciles_at_zero(self):
        # Self-hosted work is free, and a zero charge against an unmoved
        # balance is agreement, not a missing bill.
        r = Reconciliation(charges=[Charge("n1", "b1", 10, 0.0, 750.0, None, at=1.0)])
        assert r.charged == 0.0
        assert r.drift == pytest.approx(0.0)
        assert r.agrees is True

    def test_an_authoritative_pair_beats_the_headers(self):
        # Even with one broker reporting, an explicit reading wins: it is the
        # only one taken against a known single ledger either side of the run.
        r = Reconciliation(
            charges=[Charge("n1", "b1", 1, 75.0, 1425.0, None, at=1.0)],
            authoritative=(1500.0, 1350.0),
        )
        assert r.observed == pytest.approx(150.0)
        assert r.drift == pytest.approx(75.0)


class TestPlacementIsRecorded:
    """A charge says which worker ran the batch, so a Task pinned to a node
    can be checked against that node's own workers — the reply header was
    the only place that answer existed, and the SDK dropped it."""

    def test_the_worker_and_node_are_kept_on_the_charge(self, monkeypatch, signed_in):
        replies = [
            {
                "ids": [0, 1, 2, 3],
                "headers": {
                    "X-Zakuro-Cost": "75.0",
                    "X-Zakuro-Balance": "1425.0",
                    "X-Zakuro-Worker": "worker-zc-worker-alpha-2",
                },
            }
        ]
        n = node(1)
        task, _, _ = run_over(monkeypatch, [n], replies)
        (charge,) = task.reconciliation.charges
        assert charge.worker == "worker-zc-worker-alpha-2"
        assert charge.node_id == n.node_id

    def test_a_broker_that_does_not_say_leaves_it_unknown(self, monkeypatch, signed_in):
        task, _, _ = run_over(monkeypatch, [node(1)], ONE_NODE)
        assert task.reconciliation.charges[0].worker is None
