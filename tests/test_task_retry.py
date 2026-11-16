"""ZAK-0014: a batch is retried only when it provably never reached the node."""

from __future__ import annotations

import httpx
import pytest

from tests.test_task_zc_placement import frames
from zakuro import task as task_mod
from zakuro.task import Task, _decode_request_ids


def double(x):
    return x * 2


class Flaky:
    """An httpx.Client stand-in that raises `errors` before answering."""

    def __init__(self, errors, status=200):
        self.errors = list(errors)
        self.status = status
        self.posts = 0

    def __call__(self, *_a, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return False

    def post(self, url, content=b"", headers=None, **_kw):
        self.posts += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.status != 200:
            return httpx.Response(self.status, text="nope")
        return httpx.Response(
            200,
            content=frames(_decode_request_ids(content)),
            headers={"X-Zakuro-Cost": "0.003", "X-Zakuro-Batch-Id": "b1"},
        )


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ZAKURO_API_KEY", "k-test")
    monkeypatch.setattr(task_mod, "local_broker", lambda *a, **k: "127.0.0.1:9000")
    monkeypatch.setattr(task_mod.time, "sleep", lambda _s: None)


def run(monkeypatch, fake, **kw):
    monkeypatch.setattr(httpx, "Client", fake)
    task = Task(**kw)
    for i in range(3):
        task.add(double, i)
    return task, task.run()


def test_connect_error_is_retried_and_results_arrive(monkeypatch):
    fake = Flaky([httpx.ConnectError("refused")])
    task, results = run(monkeypatch, fake)
    assert fake.posts == 2
    assert [r.ok for r in results] == [True] * 3
    assert [r.value for r in results] == [0, 2, 4]


def test_reconciliation_counts_the_batch_once(monkeypatch):
    fake = Flaky([httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")])
    task, _ = run(monkeypatch, fake)
    assert fake.posts == 3
    assert len(task.reconciliation.charges) == 1
    assert task.cost == pytest.approx(0.003)


def test_retry_budget_is_bounded(monkeypatch):
    fake = Flaky([httpx.ConnectError("x")] * 10)
    _, results = run(monkeypatch, fake, connect_retries=2)
    assert fake.posts == 3
    assert all(not r.ok and "unreachable" in r.error for r in results)


def test_retries_can_be_disabled(monkeypatch):
    fake = Flaky([httpx.ConnectError("x")])
    run(monkeypatch, fake, connect_retries=0)
    assert fake.posts == 1


@pytest.mark.parametrize("status", [402, 404, 500, 503])
def test_http_status_is_never_retried(monkeypatch, status):
    fake = Flaky([], status=status)
    _, results = run(monkeypatch, fake)
    assert fake.posts == 1
    assert all(r.error.startswith(f"HTTP {status}") for r in results)


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.RemoteProtocolError("p")])
def test_possibly_delivered_failures_are_never_retried(monkeypatch, exc):
    fake = Flaky([exc])
    _, results = run(monkeypatch, fake)
    assert fake.posts == 1
    assert all(not r.ok for r in results)


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")]
)
def test_each_never_sent_failure_is_retried_on_its_own(monkeypatch, exc):
    fake = Flaky([exc])
    task, results = run(monkeypatch, fake)
    assert fake.posts == 2
    assert [r.value for r in results] == [0, 2, 4]
    assert len(task.reconciliation.charges) == 1


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x")]
)
def test_exhausted_budget_surfaces_the_error_for_every_job(monkeypatch, exc):
    fake = Flaky([exc] * 10)
    task, results = run(monkeypatch, fake, connect_retries=2)
    assert fake.posts == 3  # first attempt + exactly 2 re-sends
    assert len(results) == 3
    for r in results:
        assert not r.ok
        assert f"unreachable: {type(exc).__name__}" in r.error
    assert task.reconciliation.charges == []
    assert task.cost == 0.0


def test_backoff_is_exponential_with_full_jitter(monkeypatch):
    sleeps = []
    monkeypatch.setattr(task_mod.time, "sleep", sleeps.append)
    fake = Flaky([httpx.ConnectError("x")] * 10)
    run(monkeypatch, fake, connect_retries=2, retry_backoff=0.5)
    assert len(sleeps) == 2  # no sleep after the last attempt
    assert 0 <= sleeps[0] <= 0.5
    assert 0 <= sleeps[1] <= 1.0


@pytest.mark.parametrize("status", [402, 500])
def test_possibly_charged_http_status_is_not_retried_and_adds_no_charge(monkeypatch, status):
    # The reply may follow a charge: the batch must never be re-sent (a second
    # send could double-charge) and the SDK must not invent a Charge for it.
    fake = Flaky([], status=status)
    task, results = run(monkeypatch, fake)
    assert fake.posts == 1
    assert all(r.error.startswith(f"HTTP {status}") for r in results)
    assert task.reconciliation.charges == []
    assert task.cost == 0.0


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.RemoteProtocolError("p")])
def test_failure_after_a_connect_retry_is_not_retried_again(monkeypatch, exc):
    # First attempt never connected (retried), second may have been charged.
    fake = Flaky([httpx.ConnectError("x"), exc])
    task, results = run(monkeypatch, fake, connect_retries=5)
    assert fake.posts == 2
    assert all(not r.ok and "unreachable" in r.error for r in results)
    assert task.reconciliation.charges == []
