"""The thread pool that runs a worker's jobs.

``await loop.run_in_executor(ThreadPoolExecutor, fn, arg)`` costs, per job, a
``concurrent.futures.Future`` with its condition variable, an asyncio future
chained to it, and the callbacks between them. On a 4-CPU executor host at 64
jobs in flight that hand-off was 0.15 ms of the worker's ~0.55 ms of CPU per
job. :class:`JobPool` keeps the threads and the queue and drops the rest: a
job is put on one queue with the loop's own future, and its result is set on
that future from the loop.

It is also a :class:`concurrent.futures.Executor` (``submit``, ``shutdown``)
for code that wants one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import queue
import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# A queued job: what to call, where its outcome goes, whether its caller has
# gone, and how to cancel it unrun. ``None`` stops a thread.
_Job = tuple[
    Callable[[], Any],
    Callable[[Any], None],
    Callable[[BaseException], None],
    Callable[[], bool],
    Callable[[], None],
]


def _set_result(fut: asyncio.Future[Any], value: Any) -> None:
    if not fut.cancelled():
        fut.set_result(value)


def _set_exception(fut: asyncio.Future[Any], exc: BaseException) -> None:
    if not fut.cancelled():
        fut.set_exception(exc)


def _post(loop: asyncio.AbstractEventLoop, callback: Callable[..., Any], *args: Any) -> None:
    """Have the loop's thread run ``callback(*args)``. A loop that closed while
    the job ran has nobody waiting for it."""
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(callback, *args)


class JobPool(concurrent.futures.Executor):
    """``max_workers`` threads draining one queue, started at once."""

    def __init__(self, max_workers: int, thread_name_prefix: str = "zakuro-job") -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        self._queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = [
            threading.Thread(target=self._drain, name=f"{thread_name_prefix}-{i}", daemon=True)
            for i in range(max_workers)
        ]
        for t in self._threads:
            t.start()

    def _put(self, job: _Job) -> None:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._queue.put(job)

    def _drain(self) -> None:
        while (job := self._queue.get()) is not None:
            call, done, fail, abandoned, _cancel = job
            if abandoned():
                continue
            try:
                value = call()
            except BaseException as exc:  # noqa: BLE001 - the caller decides, as with ThreadPoolExecutor
                fail(exc)
            else:
                done(value)

    async def run(self, fn: Callable[[Any], T], arg: Any) -> T:
        """``fn(arg)`` on a pool thread, awaited on the running loop."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()

        # A request cancelled while its job was still queued does not run it,
        # as with run_in_executor.
        self._put(
            (
                lambda: fn(arg),
                lambda value: _post(loop, _set_result, fut, value),
                lambda exc: _post(loop, _set_exception, fut, exc),
                fut.cancelled,
                lambda: _post(loop, fut.cancel),
            )
        )
        return await fut

    def submit(
        self, fn: Callable[..., T], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[T]:
        cfut: concurrent.futures.Future[T] = concurrent.futures.Future()

        def done(value: Any) -> None:
            cfut.set_result(value)

        def fail(exc: BaseException) -> None:
            cfut.set_exception(exc)

        def abandoned() -> bool:
            return not cfut.set_running_or_notify_cancel()

        def cancel() -> None:
            cfut.cancel()

        self._put((lambda: fn(*args, **kwargs), done, fail, abandoned, cancel))
        return cfut

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        job = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if job is not None:
                        job[4]()
            for _ in self._threads:
                self._queue.put(None)
        if wait:
            for t in self._threads:
                t.join()
