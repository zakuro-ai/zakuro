"""Tests for zakuro.worker.pool.JobPool, the thread pool behind POST /execute."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from zakuro.worker.pool import JobPool


def _thread_name(_: object) -> str:
    return threading.current_thread().name


def test_a_job_runs_on_a_pool_thread_not_the_event_loop() -> None:
    """User code never runs on the loop: one that blocks would stall every
    request, and one calling asyncio.run() would fail there."""
    pool = JobPool(2)
    try:

        async def main() -> tuple[str, str]:
            return await pool.run(_thread_name, None), threading.current_thread().name

        on_pool, on_loop = asyncio.run(main())
        assert on_pool != on_loop
        assert on_pool.startswith("zakuro-job-")
    finally:
        pool.shutdown()


def test_jobs_run_side_by_side_up_to_the_pool_size() -> None:
    """As with the ThreadPoolExecutor it replaces: four sleeping jobs on four
    threads take one sleep, not four."""
    pool = JobPool(4)
    try:

        async def main() -> float:
            t = time.perf_counter()
            await asyncio.gather(*(pool.run(time.sleep, 0.2) for _ in range(4)))
            return time.perf_counter() - t

        assert asyncio.run(main()) < 0.6
    finally:
        pool.shutdown()


def test_a_job_that_raises_raises_where_it_is_awaited() -> None:
    def boom(_: object) -> None:
        raise ValueError("user code failed")

    pool = JobPool(1)
    try:

        async def main() -> None:
            await pool.run(boom, None)

        with pytest.raises(ValueError, match="user code failed"):
            asyncio.run(main())
    finally:
        pool.shutdown()


def test_the_executor_interface_still_works() -> None:
    """submit() answers a concurrent.futures.Future, for callers of the
    Executor API (and the startup/shutdown test of the app)."""
    pool = JobPool(1)
    try:
        assert pool.submit(pow, 2, 10).result(timeout=5) == 1024
        with pytest.raises(ZeroDivisionError):
            pool.submit(divmod, 1, 0).result(timeout=5)
    finally:
        pool.shutdown()


def test_no_work_is_taken_after_shutdown() -> None:
    pool = JobPool(2)
    pool.shutdown()
    with pytest.raises(RuntimeError):
        pool.submit(int)

    async def main() -> None:
        await pool.run(int, 0)

    with pytest.raises(RuntimeError):
        asyncio.run(main())


def test_shutdown_waits_for_the_job_in_hand() -> None:
    pool = JobPool(1)
    done = threading.Event()

    def slow(_: object) -> None:
        time.sleep(0.2)
        done.set()

    fut = pool.submit(slow, None)
    time.sleep(0.05)
    pool.shutdown(wait=True)
    assert done.is_set()
    assert fut.done()


def test_a_job_whose_caller_gave_up_before_it_started_is_not_run() -> None:
    """As run_in_executor did: a request cancelled while its job was still
    queued (the client went away) does not run that job."""
    pool = JobPool(1)
    gate = threading.Event()
    ran = []
    try:

        async def main() -> None:
            blocker = asyncio.ensure_future(pool.run(lambda _: gate.wait(5), None))
            queued = asyncio.ensure_future(pool.run(ran.append, "queued"))
            await asyncio.sleep(0.05)
            queued.cancel()
            await asyncio.sleep(0.05)
            gate.set()
            await blocker
            await asyncio.sleep(0.05)

        asyncio.run(main())
        assert ran == []
    finally:
        gate.set()
        pool.shutdown()


def test_shutdown_can_cancel_what_is_still_queued() -> None:
    pool = JobPool(1)
    gate = threading.Event()
    first = pool.submit(gate.wait, 5)
    queued = pool.submit(int, 7)
    time.sleep(0.05)
    threading.Timer(0.1, gate.set).start()
    pool.shutdown(wait=True, cancel_futures=True)
    assert first.result(timeout=1) is True
    assert queued.cancelled()
