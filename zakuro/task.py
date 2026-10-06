"""A Task: one or more jobs, planned once, dispatched in as few trips as possible.

On a mesh the round trip *is* the cost. A worker answers `add(2, 3)` in about a
millisecond while reaching it takes hundreds, so submitting one job per request
caps a requester at `concurrency ÷ RTT` no matter how fast the fleet is. zc's
``/execute/batch`` exists for exactly this -- many jobs per trip, one
reservation -- and the SDK was not using it, so every `@zk.fn` call paid a full
round trip on its own.

A Task fixes the shape rather than the constant:

    task = zk.Task()
    for x in range(1000):
        task.add(square, x)

    print(task.plan())      # what it intends to do -- costs nothing
    results = task.run()    # one request per node, not one per job

**Planning must not itself be chatty**, or it just moves the overhead. So the
plan is computed from the node view the SDK already caches (:func:`zakuro.nodes`,
30s TTL) and asks the mesh nothing. It is a statement of intent you can inspect
and act on before committing work, not a negotiation.

Verification is the dispatch. Each broker takes its whole share in one request
and answers with one reservation, so a plan that cannot be honoured is refused
once, by the node that would have run it, rather than after a round of
questions to every node in the mesh. For 1000 jobs across 6 nodes that is
**6 requests, not 1000, and not 1006**.

Reconciliation follows the same rule and comes free. Every reply already says
what it charged and what the account holds afterwards, so `task.reconciliation`
compares the two without a single added request -- see :mod:`zakuro.reconcile`.
Sampling *during* a long Task does cost a request per tick, so it is opt-in via
`run(reconcile_every=...)` and priced per Task, never per job.
"""

from __future__ import annotations

import json
import os
import random
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cloudpickle

from zakuro.mesh import local_broker
from zakuro.placement import OBSERVED, allocate, deal
from zakuro.reconcile import (
    BalancePoller,
    Charge,
    Reconciliation,
    dashboard_balance,
    settled_balance,
)

__all__ = ["Task", "Job", "JobResult", "Plan", "PlanStep", "Reconciliation"]

#: Batch request magic, and the caps zc enforces on the other side. Matching
#: them here turns a frame zc would reject into a local error naming the job.
_MAGIC = b"ZKB1"
_MAX_FIELD_BYTES = 64 * 1024 * 1024
_MAX_TASKS = 10_000


@dataclass
class Job:
    """One unit of work: a function and its arguments."""

    func: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    #: Per-job requirement overrides. Empty means "inherit the batch's", which
    #: is the common case and keeps a batch of identical jobs from repeating
    #: itself N times on the wire.
    requirements: dict | None = None

    def payload(self) -> bytes:
        # cloudpickle is untyped, so pin the boundary here rather than letting
        # Any leak out of a function that promises bytes.
        blob: bytes = cloudpickle.dumps(
            {"func": self.func, "args": self.args, "kwargs": self.kwargs}
        )
        return blob


@dataclass
class JobResult:
    """How one job turned out."""

    index: int
    ok: bool
    value: Any = None
    error: str | None = None


@dataclass
class PlanStep:
    """What one node has been asked to do."""

    node_id: str
    node_name: str
    endpoint: str
    job_indices: list[int]

    @property
    def jobs(self) -> int:
        return len(self.job_indices)


@dataclass
class Plan:
    """What a Task intends to do, before it does any of it.

    Derived from the cached node view, so building one costs no network at all.
    Read it, change your mind, or run it.
    """

    steps: list[PlanStep]
    total_jobs: int
    #: Requests the run will make. The number to look at: it should track the
    #: number of NODES, never the number of jobs.
    round_trips: int
    #: Nodes that were considered and left out, with the reason.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: True when zc places the jobs (an unpinned Task): the split is known only
    #: afterwards, from `Task.reconciliation.charges`.
    by_zc: bool = False

    def __str__(self) -> str:  # pragma: no cover - display only
        lines = [
            f"{self.total_jobs} job(s) over {len(self.steps)} node(s) "
            f"in {self.round_trips} request(s)"
        ]
        for s in self.steps:
            lines.append(f"  {s.node_name:26} {s.jobs:5d} job(s)")
        for name, why in self.skipped:
            lines.append(f"  {name:26}    -- skipped: {why}")
        return "\n".join(lines)


def _encode_request(entries: Sequence[tuple[int, bytes, bytes]]) -> bytes:
    """Frame jobs as zc's `ZKB1`: magic, count, then (id, requirements, payload).

    Binary rather than JSON because payloads are cloudpickle, and base64 would
    inflate every one by a third on exactly the links where bandwidth counts.
    """
    if len(entries) > _MAX_TASKS:
        raise ValueError(
            f"{len(entries)} jobs in one frame; zc accepts at most {_MAX_TASKS}. "
            "Split the Task, or let run() chunk it."
        )
    out = bytearray(_MAGIC)
    out += struct.pack("<I", len(entries))
    for job_id, requirements, payload in entries:
        for name, blob in (("requirements", requirements), ("payload", payload)):
            if len(blob) > _MAX_FIELD_BYTES:
                raise ValueError(
                    f"job {job_id}'s {name} is {len(blob)} bytes; zc caps a field at "
                    f"{_MAX_FIELD_BYTES}. A payload that large usually means a closure "
                    "captured something it did not need."
                )
        out += struct.pack("<I", job_id)
        out += struct.pack("<I", len(requirements)) + requirements
        out += struct.pack("<I", len(payload)) + payload
    return bytes(out)


def _decode_request_ids(body: bytes) -> list[int]:
    """The job ids in a `ZKB1` request (tests and diagnostics)."""
    out, off = [], 4  # b"ZKB1"
    (count,) = struct.unpack_from("<I", body, off)
    off += 4
    for _ in range(count):
        (job_id,) = struct.unpack_from("<I", body, off)
        off += 4
        (rlen,) = struct.unpack_from("<I", body, off)
        off += 4 + rlen
        (plen,) = struct.unpack_from("<I", body, off)
        off += 4 + plen
        out.append(job_id)
    return out


def _decode_results(body: bytes) -> list[tuple[int, bool, bytes]]:
    """Read result frames: `(id, status, body)`, in COMPLETION order.

    A short trailing frame is not an error -- zc streams results as they finish,
    so a partial tail simply means more is coming.
    """
    header = 4 + 1 + 4
    out: list[tuple[int, bool, bytes]] = []
    at = 0
    while len(body) - at >= header:
        job_id = struct.unpack_from("<I", body, at)[0]
        status = body[at + 4]
        if status not in (0, 1):
            raise ValueError(f"result frame for job {job_id} has status {status}")
        length = struct.unpack_from("<I", body, at + 5)[0]
        if len(body) - (at + header) < length:
            break  # partial tail
        payload = body[at + header : at + header + length]
        out.append((job_id, status == 0, bytes(payload)))
        at += header + length
    return out


def _as_float(raw: Any) -> float | None:
    """A header value as a number, or `None` when it is not one.

    Headers are strings from another machine. A broker that sends `""`, or a
    build that stops sending the header at all, must read as "did not say" —
    never as `0.0`, which in a ledger is a claim.
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _never_sent() -> tuple[type[BaseException], ...]:
    import httpx

    return (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


_NEVER_SENT = _never_sent()


class Task:
    """One or more jobs, planned together and dispatched together."""

    #: Nodes dispatched to at once. The requests go to DIFFERENT machines, so
    #: this is not load on any one of them — it is the difference between six
    #: round trips end to end and six round trips overlapped. Bounded anyway:
    #: a mesh can be large, and a thread per node stops being free.
    MAX_PARALLEL = 16

    def __init__(
        self,
        requirements: dict | None = None,
        timeout: float = 300.0,
        placement: str = "weighted",
        connect_retries: int = 2,
        retry_backoff: float = 0.25,
    ) -> None:
        self.jobs: list[Job] = []
        self.requirements = requirements or {}
        self.timeout = timeout
        #: Re-sends of a batch whose connection could not be made (ZAK-0014).
        #: Never applied once a request may have been delivered.
        self.connect_retries = max(0, connect_retries)
        #: Base of the exponential backoff between those re-sends, seconds.
        self.retry_backoff = retry_backoff
        #: ``"weighted"`` splits by observed throughput and falls back to an
        #: even split until there is something to observe; ``"even"`` pins the
        #: split regardless, for a reproducible plan.
        self.placement = placement
        #: Total credits the brokers charged for this task, summed across
        #: batches. Zero is a real answer, and correct on your own hardware.
        self.cost: float = 0.0
        #: What the brokers charged, against what the balance actually did.
        #: Populated by `run()` from headers the replies already carry, so it
        #: costs nothing. See :mod:`zakuro.reconcile`.
        self.reconciliation = Reconciliation()
        self._plan: Plan | None = None
        #: Guards `cost` and the charge list while nodes answer in parallel.
        self._lock = threading.Lock()
        #: Decision-log destination; ``None`` (the default) means no I/O at all.
        self._log_path: Path | None = None
        self._log_lock = threading.Lock()
        #: Per-thread hand-off from `_dispatch` (cost, charges) to the logger.
        self._batch_local = threading.local()

    def add(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Task:
        """Queue one job. Returns self, so calls chain."""
        self.jobs.append(Job(func=func, args=args, kwargs=kwargs))
        return self

    def extend(self, func: Callable[..., Any], arg_sets: Iterable[tuple]) -> Task:
        """Queue the same function over many argument tuples."""
        for a in arg_sets:
            self.jobs.append(Job(func=func, args=tuple(a)))
        return self

    def __len__(self) -> int:
        return len(self.jobs)

    # -- decision log -------------------------------------------------------

    def enable_decision_log(self, path: str | None = None) -> Path:
        """Append one JSON row per dispatched batch (ZAK-0019). Off by default.

        Same file and path rules as ``AdaptiveCompute.enable_decision_log``
        (``$ZAKURO_DECISION_LOG`` or ``~/.zakuro/allocator.jsonl``); rows carry
        ``"kind": "task_batch"``. Never records payloads or the API key. The
        file is append-only and unrotated: one row per batch, not per job.
        """
        if path is None:
            path = os.environ.get("ZAKURO_DECISION_LOG") or str(
                Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or "/tmp")
                / ".zakuro"
                / "allocator.jsonl"
            )
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_path = log_path
        return log_path

    def disable_decision_log(self) -> None:
        self._log_path = None

    def _log_batch(
        self,
        step: PlanStep,
        indices: list[int],
        results: list[JobResult | None],
        elapsed: float,
        cost: float,
        charges: list[Charge],
    ) -> None:
        """Best-effort append of one batch row; never raises."""
        if self._log_path is None:
            return
        try:
            done = [results[i] for i in indices]
            ok_jobs = sum(1 for r in done if r is not None and r.ok)
            err = next((r.error for r in done if r is not None and r.error), None)
            if ok_jobs == len(indices):
                outcome = "ok"
            elif err and "unreachable" in err:
                outcome = "unreachable"
            else:
                outcome = "partial" if ok_jobs else "failed"
            first = charges[0] if charges else None
            row = {
                "kind": "task_batch",
                "t": time.time(),
                "node_id": step.node_id,
                "node_name": step.node_name,
                "worker": first.worker if first else None,
                "batch_id": first.batch_id if first else None,
                "jobs": len(indices),
                "ok_jobs": ok_jobs,
                "cost": cost,
                "remaining": first.remaining if first else None,
                "service_ms": first.service_ms if first else None,
                "placement": "mesh" if step.node_id == "zc" else "pinned",
                "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "outcome": outcome,
                "elapsed_secs": elapsed,
                "error": err[:200] if err else None,
            }
            line = json.dumps(row, default=str) + "\n"
            with self._log_lock, open(self._log_path, "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            # Observability must never break the dispatch path.
            pass

    def _dispatch_logged(
        self, step: PlanStep, indices: list[int], results: list[JobResult | None]
    ) -> None:
        """`_dispatch`, then one decision-log row for the batch."""
        if self._log_path is None:
            self._dispatch(step, indices, results)
            return
        self._batch_local.outcome = (0.0, [])
        started = time.monotonic()
        try:
            self._dispatch(step, indices, results)
        finally:
            cost, charges = self._batch_local.outcome
            self._log_batch(step, indices, results, time.monotonic() - started, cost, charges)

    # -- planning -----------------------------------------------------------

    def plan(self, nodes: Sequence[Any] | None = None) -> Plan:
        """Decide where the jobs go. Costs no network.

        Uses the cached node view, so planning a thousand jobs is as cheap as
        planning one. A node with no workers of its own is left out with a
        reason rather than silently dropped -- on a mesh the interesting
        question is usually why something was NOT used.
        """
        if nodes is None:
            # Unpinned: zc places it. The split is known only afterwards, in
            # `reconciliation.charges`.
            endpoint = local_broker()
            if endpoint is None:
                self._plan = Plan(
                    steps=[],
                    total_jobs=len(self.jobs),
                    round_trips=0,
                    skipped=[("local broker", "no zc broker is running on this machine")],
                )
                return self._plan
            self._plan = Plan(
                steps=[
                    PlanStep(
                        node_id="zc",
                        node_name="local broker (zc places)",
                        endpoint=endpoint,
                        job_indices=list(range(len(self.jobs))),
                    )
                ],
                total_jobs=len(self.jobs),
                round_trips=-(-len(self.jobs) // _MAX_TASKS) if self.jobs else 0,
                by_zc=True,
            )
            return self._plan

        from zakuro.mesh import nodes as discover

        candidates = list(nodes) if nodes is not None else discover()
        usable, skipped = [], []
        for n in candidates:
            if not getattr(n, "workers", None):
                skipped.append((n.name, "no workers of its own"))
            else:
                usable.append(n)

        if not usable:
            self._plan = Plan(steps=[], total_jobs=len(self.jobs), round_trips=0, skipped=skipped)
            return self._plan

        # Weighted by what these nodes have actually been seen to do, which
        # costs nothing: the rates come from batches this process already ran
        # (see :mod:`zakuro.placement`). With nothing measured yet the weights
        # are None and the split is even -- exactly the round-robin this
        # started as. A Task waits for all of its batches, so an even split
        # across an uneven mesh makes every Task as slow as its slowest node.
        #
        # Still a purely local decision. The moment planning asks the mesh
        # anything, the round trips it saves are the round trips it spends.
        steps = [
            PlanStep(node_id=n.node_id, node_name=n.name, endpoint=n.endpoint, job_indices=[])
            for n in usable
        ]
        weights = (
            None if self.placement == "even" else OBSERVED.weights([n.node_id for n in usable])
        )
        shares = allocate(len(self.jobs), len(steps), weights)
        for step, indices in zip(steps, deal(len(self.jobs), shares), strict=True):
            step.job_indices.extend(indices)
        steps = [s for s in steps if s.job_indices]

        # One request per node, plus one more per node for every full frame
        # beyond zc's cap.
        trips = sum(-(-s.jobs // _MAX_TASKS) for s in steps)
        self._plan = Plan(
            steps=steps, total_jobs=len(self.jobs), round_trips=trips, skipped=skipped
        )
        return self._plan

    # -- running ------------------------------------------------------------

    def run(
        self,
        nodes: Sequence[Any] | None = None,
        reconcile_every: float | None = None,
        reconcile: bool = False,
    ) -> list[JobResult]:
        """Dispatch the plan and collect results, in submission order.

        One request per node (per full frame), not one per job.

        `reconcile` checks the charges against the **dashboard**, which is the
        only authority on what an account holds. A broker's own number is not
        one: credits are sharded and each broker caches its view, so on a live
        9-broker mesh four different balances were reported at the same
        instant, 0.685 zkcr apart, and none of them moved for 45s after a
        Task they had charged for. The dashboard's did.

        It is off by default because it is not free in the way the charges
        are. Brokers forward to the dashboard in batches, so the number has to
        be given time to settle — about 14s on the measured mesh — and that is
        latency added to a Task that may itself have taken 4. Two requests
        plus a wait, per Task, when you want the answer.

        Without it the per-broker charges are still recorded from the reply
        headers, free, and `reconciliation.charged` is still what the brokers
        said they took.

        `reconcile_every` additionally samples the balance on a timer, which
        costs a request per tick — again one, against a single broker — so it
        is worth it only for a Task long enough that finding out at the end is
        finding out too late.
        """
        plan = self._plan or self.plan(nodes)
        if not plan.steps:
            why = "; ".join(f"{n}: {w}" for n, w in plan.skipped) or "no nodes reachable"
            raise RuntimeError(f"nothing to run this Task on ({why})")

        results: list[JobResult | None] = [None] * len(self.jobs)
        self.cost = 0.0
        self.reconciliation = Reconciliation()

        endpoint = plan.steps[0].endpoint
        # The dashboard, not a broker. See `reconcile` above.
        before = dashboard_balance() if reconcile else None
        poller = (
            BalancePoller(lambda: self._balance(endpoint), reconcile_every)
            if reconcile_every
            else None
        )
        units = [(step, chunk) for step in plan.steps for chunk in self._chunks(step.job_indices)]
        if poller is None:
            self._dispatch_all(units, results)
        else:
            with poller:
                self._dispatch_all(units, results)
            self.reconciliation.samples = poller.samples

        if before is not None:
            # Wait for the dashboard to stop moving rather than a fixed sleep:
            # too short is wrong, too long is unusable.
            after = settled_balance(before, self.cost)
            if after is not None:
                self.reconciliation.authoritative = (before, after)

        # A job whose frame never came back is a failure, not a silent None.
        return [
            r
            if r is not None
            else JobResult(index=i, ok=False, error="no result frame returned for this job")
            for i, r in enumerate(results)
        ]

    @staticmethod
    def _chunks(indices: list[int]) -> Iterable[list[int]]:
        for i in range(0, len(indices), _MAX_TASKS):
            yield indices[i : i + _MAX_TASKS]

    def _dispatch_all(
        self, units: list[tuple[PlanStep, list[int]]], results: list[JobResult | None]
    ) -> None:
        """Send every batch, overlapping the ones bound for different nodes.

        Batching fixed the number of requests; this fixes when they happen.
        Six nodes used to mean six round trips end to end, which on a mesh
        where the trip is the cost made the fleet no faster than its slowest
        link times its node count. They go to different machines, so there is
        nothing to serialise.
        """
        if len(units) == 1:
            self._dispatch_logged(*units[0], results)
            return
        with ThreadPoolExecutor(max_workers=min(len(units), self.MAX_PARALLEL)) as pool:
            for future in [pool.submit(self._dispatch_logged, s, c, results) for s, c in units]:
                # `_dispatch` reports failure through `results` and does not
                # raise; this re-raises anything that escaped it anyway rather
                # than letting a silently dead thread look like success.
                future.result()

    def _post_batch(self, host: str, url: str, content: bytes, headers: dict[str, str]) -> Any:
        """POST one batch, retrying only where it provably never arrived.

        See ZAK-0014. A connect failure or a pool-acquire timeout means no
        request bytes were sent, so the batch cannot have run or been charged
        and re-sending it is safe. Anything after that (a read timeout, a
        dropped connection, any HTTP status) may have executed the pickled
        jobs, so it is surfaced to the caller instead of retried.
        """
        import httpx

        from zakuro.mesh import proxy_for

        attempt = 0
        while True:
            try:
                with httpx.Client(proxy=proxy_for(host), timeout=self.timeout) as client:
                    return client.post(url, content=content, headers=headers)
            except _NEVER_SENT:
                if attempt >= self.connect_retries:
                    raise
                # Exponential backoff with full jitter, so nodes that blip
                # together do not all come back together.
                time.sleep(random.uniform(0, self.retry_backoff * (2**attempt)))
                attempt += 1

    def _dispatch(
        self, step: PlanStep, indices: list[int], results: list[JobResult | None]
    ) -> None:

        from zakuro.mesh import api_key

        entries = []
        for i in indices:
            job = self.jobs[i]
            per_job = json.dumps(job.requirements).encode() if job.requirements else b""
            entries.append((i, per_job, job.payload()))

        host = step.endpoint.split(":")[0]
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Zakuro-Requirements": json.dumps(self._batch_requirements()),
        }
        key = api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if step.node_id == "zc":
            headers["X-Zakuro-Placement"] = "mesh"

        started = time.monotonic()
        try:
            reply = self._post_batch(
                host, f"http://{step.endpoint}/execute/batch", _encode_request(entries), headers
            )
        except Exception as exc:
            # One unreachable node must not lose the work every other node
            # already did. It used to: the exception escaped `run()` and took
            # the whole result set with it, including the batches that landed.
            for i in indices:
                results[i] = JobResult(
                    index=i,
                    ok=False,
                    error=f"{step.node_name} unreachable: {type(exc).__name__}: {exc}",
                )
            return

        if reply.status_code != 200:
            detail = reply.text[:160].strip()
            for i in indices:
                results[i] = JobResult(
                    index=i, ok=False, error=f"HTTP {reply.status_code}: {detail}"
                )
            return

        # A malformed header must not lose the results that came with it, so
        # every one of these is read defensively and independently.
        cost = _as_float(reply.headers.get("X-Zakuro-Cost")) or 0.0
        placed = reply.headers.get("X-Zakuro-Placement") == "mesh"
        if placed:
            # zc placed it: one charge per node, from the node's own broker.
            try:
                charge_entries = json.loads(reply.headers.get("X-Zakuro-Charges") or "[]")
            except ValueError:
                charge_entries = []
            if not isinstance(charge_entries, list):
                charge_entries = []
            # Read defensively: an entry we do not understand costs that
            # charge, never the results that came with the reply.
            charges = [
                Charge(
                    node_name=str(entry.get("name") or ""),
                    batch_id=str(entry.get("batch_id") or ""),
                    jobs=int(_as_float(entry.get("jobs")) or 0),
                    cost=_as_float(entry.get("cost")) or 0.0,
                    remaining=_as_float(entry.get("remaining")),
                    service_ms=_as_float(entry.get("service_ms")),
                    at=time.time(),
                    worker=str(entry.get("worker")) if entry.get("worker") else None,
                    node_id=str(entry.get("node") or ""),
                )
                for entry in charge_entries
                if isinstance(entry, dict)
            ]
        else:
            charges = [
                Charge(
                    node_name=step.node_name,
                    batch_id=reply.headers.get("X-Zakuro-Batch-Id", ""),
                    jobs=len(indices),
                    cost=cost,
                    remaining=_as_float(reply.headers.get("X-Zakuro-Credits-Remaining")),
                    service_ms=_as_float(reply.headers.get("X-Zakuro-Service-Ms")),
                    at=time.time(),
                    worker=reply.headers.get("X-Zakuro-Worker") or None,
                    node_id=step.node_id,
                )
            ]
        with self._lock:
            self.cost += cost
            self.reconciliation.charges.extend(charges)
        self._batch_local.outcome = (cost, charges)  # read by `_dispatch_logged`

        # Throughput is observed only where the SDK chose the node (a pinned
        # split); an unpinned batch was placed by zc.
        if step.node_id != "zc":
            OBSERVED.record(step.node_id, len(indices), time.monotonic() - started)

        for job_id, ok, body in _decode_results(reply.content):
            if not (0 <= job_id < len(self.jobs)):
                continue  # a frame for a job we did not send
            if ok:
                try:
                    value = cloudpickle.loads(body)
                except Exception as exc:
                    results[job_id] = JobResult(
                        index=job_id, ok=False, error=f"undecodable result: {exc}"
                    )
                    continue
                if isinstance(value, BaseException):
                    # The worker answered, with the job's exception as its
                    # result: the job failed (a crashed process pool, say).
                    results[job_id] = JobResult(
                        index=job_id, ok=False, error=f"{type(value).__name__}: {value}"
                    )
                else:
                    results[job_id] = JobResult(index=job_id, ok=True, value=value)
            else:
                results[job_id] = JobResult(
                    index=job_id,
                    ok=False,
                    error=body.decode("utf-8", "replace")[:200] or "job failed",
                )

    def _balance(self, endpoint: str) -> float | None:
        """The account balance, from one broker, in one request.

        Used only by `reconcile_every`. The end-of-run reconciliation does not
        call this — it reads the balance off headers the replies already
        carried, which is why it is free.
        """
        import httpx

        from zakuro.mesh import api_key, proxy_for

        key = api_key()
        if not key:
            return None
        host = endpoint.split(":")[0]
        try:
            with httpx.Client(proxy=proxy_for(host), timeout=10.0) as client:
                reply = client.get(
                    f"http://{endpoint}/me",
                    headers={"Authorization": f"Bearer {key}"},
                )
            if reply.status_code != 200:
                return None
            return _as_float(reply.json().get("balance"))
        except Exception:
            # An observation of the run must never be what fails it.
            return None

    def _batch_requirements(self) -> dict:
        from zakuro.processors.broker import DEFAULT_MEMORY_BYTES

        base = {
            "cpus": 0.1,
            "memory_bytes": DEFAULT_MEMORY_BYTES,
            "gpus": 0,
            "estimated_duration_secs": 1.0,
            "timeout_secs": float(self.timeout),
            # A cloudpickled payload runs only on the CPython major.minor that
            # built it; zc routes on this so a job never lands on a worker
            # whose interpreter would crash running it.
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        }
        base.update(self.requirements)
        return base
