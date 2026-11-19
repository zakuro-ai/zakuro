"""An unpinned Task is placed by zc, not by the SDK."""

from __future__ import annotations

import json
import struct

import cloudpickle
import httpx
import pytest

import zakuro as zk
from zakuro import task as task_mod


def double(x):
    return x * 2


def frames(ids):
    body = b""
    for i in ids:
        payload = cloudpickle.dumps(i * 2)
        body += struct.pack("<I", i) + bytes([0]) + struct.pack("<I", len(payload)) + payload
    return body


class Recorder:
    def __init__(self, reply_headers):
        self.reply_headers = reply_headers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, *_a, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return False

    def post(self, url, content=b"", headers=None, **_kw):
        self.calls.append((url, dict(headers or {})))
        from zakuro.task import _decode_request_ids  # noqa: F401 (defined in Step 3)

        ids = _decode_request_ids(content)
        return httpx.Response(200, content=frames(ids), headers=self.reply_headers)


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "k-test")
    monkeypatch.setattr(task_mod, "local_broker", lambda *a, **k: "127.0.0.1:9000")


def test_an_unpinned_task_is_one_request_to_the_local_broker(monkeypatch, local):
    charges = [
        {
            "node": "zc://node-aa",
            "name": "alpha-1",
            "batch_id": "b1",
            "jobs": 3,
            "cost": 0.45,
            "remaining": 1350.0,
            "worker": "w-alpha-1",
            "service_ms": 10.0,
        },
        {
            "node": "zc://node-bb",
            "name": "beta-1",
            "batch_id": "b2",
            "jobs": 2,
            "cost": 0.3,
            "remaining": None,
            "worker": "w-beta-1",
            "service_ms": None,
        },
    ]
    rec = Recorder(
        {
            "X-Zakuro-Placement": "mesh",
            "X-Zakuro-Cost": "0.750000",
            "X-Zakuro-Charges": json.dumps(charges),
        }
    )
    monkeypatch.setattr(httpx, "Client", rec)
    monkeypatch.setattr(
        zk, "nodes", lambda *a, **k: pytest.fail("the SDK must not enumerate nodes")
    )

    t = zk.Task()
    t.extend(double, [(i,) for i in range(5)])
    results = t.run()

    assert [r.value for r in results] == [0, 2, 4, 6, 8]
    assert len(rec.calls) == 1
    url, headers = rec.calls[0]
    assert url == "http://127.0.0.1:9000/execute/batch"
    assert headers["X-Zakuro-Placement"] == "mesh"
    got = [(c.node_name, c.node_id, c.worker, c.jobs) for c in t.reconciliation.charges]
    assert got == [
        ("alpha-1", "zc://node-aa", "w-alpha-1", 3),
        ("beta-1", "zc://node-bb", "w-beta-1", 2),
    ]
    assert abs(t.cost - 0.75) < 1e-9


def test_plan_without_nodes_says_zc_places(local):
    t = zk.Task()
    t.add(double, 1)
    plan = t.plan()
    assert plan.by_zc
    assert plan.steps[0].node_name == "local broker (zc places)"
    assert plan.round_trips == 1


def test_an_old_broker_is_accepted_without_replanning(monkeypatch, local):
    rec = Recorder(
        {
            "X-Zakuro-Cost": "0.150000",
            "X-Zakuro-Balance": "1350.0",
            "X-Zakuro-Batch-Id": "old",
            "X-Zakuro-Worker": "w-local",
        }
    )
    monkeypatch.setattr(httpx, "Client", rec)
    t = zk.Task()
    t.add(double, 0)  # the stub answers job id i with i * 2
    results = t.run()
    assert results[0].ok and results[0].value == 0
    assert len(rec.calls) == 1, "no client-side re-plan"
    (c,) = t.reconciliation.charges
    assert c.worker == "w-local" and c.jobs == 1


def test_pinned_plans_are_unchanged(local):
    from zakuro.mesh import NodeInfo

    n = NodeInfo(endpoint="192.0.2.2:9000", node_id="zc://node-bb", name="beta-1")
    n.workers = {"w-beta-1"}
    t = zk.Task()
    t.add(double, 1)
    plan = t.plan(nodes=[n])
    assert not plan.by_zc
    assert plan.steps[0].endpoint == "192.0.2.2:9000"


def test_malformed_charge_entries_do_not_lose_the_results(monkeypatch, local):
    """A charge entry that is not what we expect must cost the charge, never
    the results that came with the reply."""
    bad = json.dumps([{"name": "a", "node": "zc://node-a", "cost": None, "jobs": None}, "junk", 5])
    rec = Recorder({"X-Zakuro-Placement": "mesh", "X-Zakuro-Cost": "0", "X-Zakuro-Charges": bad})
    monkeypatch.setattr(httpx, "Client", rec)
    t = zk.Task()
    t.add(double, 0)
    results = t.run()
    assert results[0].ok
    (c,) = t.reconciliation.charges
    assert (c.node_name, c.cost, c.jobs) == ("a", 0.0, 0)


def test_the_local_broker_is_found_once_per_process(monkeypatch):
    """Found once and remembered: every unpinned plan() used to re-probe up
    to eleven ports."""
    from zakuro import mesh

    mesh._local_broker = None
    probes = []

    def fake_get_json(url, timeout, headers=None):
        probes.append(url)
        return {"service": "zakuro-broker"} if ":9001/" in url else None

    monkeypatch.setattr(mesh, "_get_json", fake_get_json)
    assert mesh.local_broker() == "127.0.0.1:9001"
    first = len(probes)
    assert mesh.local_broker() == "127.0.0.1:9001"
    assert len(probes) == first, "cached"
    mesh._local_broker = None


def test_reconciliation_counts_batches_not_requests():
    """zc places one request as several node batches; the summary must not
    call them requests."""
    from zakuro.reconcile import Charge, Reconciliation

    rec = Reconciliation(
        charges=[
            Charge(
                node_name=n, batch_id=n, jobs=1, cost=0.15, remaining=None, service_ms=None, at=0.0
            )
            for n in ("a", "b")
        ]
    )
    assert "over 2 node batch(es)" in str(rec)


def test_a_task_says_which_python_built_its_payload():
    """A cloudpickled payload runs only on the same CPython major.minor, so
    the Task tells zc which one built it (zc routes on `python`)."""
    import sys

    t = zk.Task()
    assert t._batch_requirements()["python"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert zk.Task(requirements={"python": "3.13"})._batch_requirements()["python"] == "3.13"


def test_a_job_that_returned_an_exception_is_a_failure(monkeypatch, local):
    """A worker that answers 200 with an exception as the result (a crashed
    process pool, say) failed that job; reporting it as ok hid it."""

    class Boom(Recorder):
        def post(self, url, content=b"", headers=None, **_kw):
            self.calls.append((url, dict(headers or {})))
            payload = cloudpickle.dumps(RuntimeError("process pool terminated abruptly"))
            frame = struct.pack("<I", 0) + bytes([0]) + struct.pack("<I", len(payload)) + payload
            return httpx.Response(200, content=frame, headers=self.reply_headers)

    monkeypatch.setattr(httpx, "Client", Boom({"X-Zakuro-Cost": "0.15"}))
    t = zk.Task()
    t.add(double, 0)
    (r,) = t.run()
    assert not r.ok
    assert "process pool terminated abruptly" in r.error
