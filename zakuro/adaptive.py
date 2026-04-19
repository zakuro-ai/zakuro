"""Adaptive, context-aware compute allocation.

Analogous to how Adam uses running estimates of the first and second
moments of the gradient to adjust step sizes, ``AdaptiveCompute`` tracks
running estimates of each worker's latency and its variance, plus the
in-flight queue depth, to route each call to the worker with the lowest
*expected* time-to-completion.

Usage::

    import zakuro as zk

    adaptive = zk.AdaptiveCompute(
        workers=[
            zk.Compute(uri="quic://mac:4433"),
            zk.Compute(uri="quic://x399:4434"),
        ],
        # β₁ / β₂ mirror Adam's moment-EMA decay rates.
        beta1=0.9,
        beta2=0.999,
    )

    @zk.fn
    def score(x): ...

    result = score.to(adaptive)(42)   # picks worker minimising expected wall time
    adaptive.stats()                   # per-worker latency EMA, queue depth, etc.
    if adaptive.is_backpressured():
        # every worker's expected time-to-serve exceeds the cap — caller may
        # choose to subsample, drop, or run locally.
        ...

``is_backpressured`` is intended to let downstream schedulers (like
Sakura's HF callback) avoid piling requests on saturated workers; in the
training loop that means "skip this epoch's eval" rather than "let the
whole pipeline stall".

The allocator is **soft**: when the expected times of the best workers
are within ``softmax_temperature`` of each other, the choice is made
probabilistically (weighted by ``softmax(-expected_time / τ)``). This
avoids pathological cases where a single pinned worker would monopolise
the queue before its latency estimate has time to degrade.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from zakuro.compute import Compute


def _identity(x: Any) -> Any:
    """Module-level identity used as the default warmup probe.

    Kept at module scope so it cloudpickles cleanly on the worker side
    (local / lambda functions don't) and so subsequent calls reuse the
    worker-side module cache.
    """
    return x


# Small sentinel passed as the probe argument.  Empty string keeps the payload
# under a dozen bytes so warmup latency is dominated by network RTT rather
# than transfer.
_WARMUP_TOKEN = ""


class _WorkerStats:
    """Per-worker running statistics in the Adam style.

    Attributes
    ----------
    m:
        EMA of observed latency (first moment). Bias-corrected via ``m_hat``.
    v:
        EMA of squared-deviation (second moment). Bias-corrected via ``v_hat``.
    step:
        Number of completed dispatches. Used for Adam's bias correction.
    queue:
        Number of in-flight dispatches not yet returned.
    failures:
        Count of exceptions while executing on this worker.
    last_latency:
        Most recent completed-dispatch latency.
    """

    __slots__ = ("m", "v", "step", "queue", "failures", "last_latency")

    def __init__(self) -> None:
        self.m: float = 0.0
        self.v: float = 0.0
        self.step: int = 0
        self.queue: int = 0
        self.failures: int = 0
        self.last_latency: Optional[float] = None

    def m_hat(self, beta1: float) -> float:
        """Bias-corrected first moment. Zero until the first completed call."""
        if self.step == 0:
            return 0.0
        return self.m / (1.0 - beta1**self.step)

    def v_hat(self, beta2: float) -> float:
        """Bias-corrected second moment. Zero until the first completed call."""
        if self.step == 0:
            return 0.0
        return self.v / (1.0 - beta2**self.step)

    def to_dict(self, beta1: float, beta2: float) -> dict[str, Any]:
        return {
            "step": self.step,
            "queue": self.queue,
            "failures": self.failures,
            "latency_ema": self.m_hat(beta1),
            "latency_var": self.v_hat(beta2),
            "last_latency": self.last_latency,
        }


class AdaptiveCompute:
    """Context-aware, Adam-style allocator across multiple workers.

    Parameters
    ----------
    workers:
        Iterable of :class:`~zakuro.compute.Compute` instances. The caller owns
        their lifecycle (connect/disconnect is driven by the processors).
    beta1:
        EMA decay for the latency first moment. Higher ⇒ more history.
    beta2:
        EMA decay for the latency second moment (variance tracking).
    softmax_temperature:
        Scale for soft (probabilistic) worker selection. Set to ``0`` for
        greedy argmin. Measured in seconds — when two workers' expected
        times-to-complete differ by ≪ ``τ`` the choice is near-uniform.
    initial_latency:
        Latency to assume for a worker before any observations land.
        Slightly pessimistic (e.g. 1 s) is a safe default: without this the
        bias-corrected EMA starts at 0 and would send the first traffic
        burst to a single arbitrary worker.
    backpressure_threshold:
        ``is_backpressured`` returns ``True`` when *every* worker's
        expected time-to-serve exceeds this many seconds.
    """

    def __init__(
        self,
        workers: Iterable["Compute"],
        *,
        beta1: float = 0.9,
        beta2: float = 0.999,
        softmax_temperature: float = 0.0,
        initial_latency: float = 1.0,
        backpressure_threshold: float = 30.0,
    ) -> None:
        self._workers: list["Compute"] = list(workers)
        if not self._workers:
            raise ValueError("AdaptiveCompute requires at least one worker.")
        if not (0.0 <= beta1 < 1.0) or not (0.0 <= beta2 < 1.0):
            raise ValueError("beta1, beta2 must be in [0, 1).")

        self._beta1 = beta1
        self._beta2 = beta2
        self._tau = float(softmax_temperature)
        self._initial_latency = float(initial_latency)
        self._backpressure = float(backpressure_threshold)

        self._stats = [_WorkerStats() for _ in self._workers]
        self._lock = threading.Lock()

    # .................................................................. API

    @property
    def workers(self) -> list["Compute"]:
        """Read-only view of the worker pool."""
        with self._lock:
            return list(self._workers)

    @property
    def backpressure_threshold(self) -> float:
        """Current backpressure cap, in seconds."""
        return self._backpressure

    @backpressure_threshold.setter
    def backpressure_threshold(self, value: float) -> None:
        if value < 0:
            raise ValueError("backpressure_threshold must be non-negative")
        self._backpressure = float(value)

    def stats(self) -> list[dict[str, Any]]:
        """Snapshot of per-worker stats, bias-corrected."""
        with self._lock:
            return [s.to_dict(self._beta1, self._beta2) for s in self._stats]

    def is_backpressured(self) -> bool:
        """True when the best worker's expected time-to-serve exceeds the cap."""
        with self._lock:
            return self._best_expected_time() > self._backpressure

    def pick(self) -> int:
        """Return the index of the worker to dispatch to next.

        Argmin of expected time-to-complete when ``softmax_temperature == 0``;
        otherwise samples softmax-weighted over expected times.
        """
        with self._lock:
            return self._pick_locked()

    # ........................................................ node lifecycle

    def add_worker(self, compute: "Compute") -> int:
        """Admit a new worker into the pool. Returns its index.

        The fresh worker is seeded with a bootstrap latency prior equal to
        the current mesh-median observed latency — this prevents "greedy
        stampede" onto a new (unverified) worker before it proves itself,
        while still letting it earn traffic on its first observation.
        """
        with self._lock:
            median = self._mesh_median_latency_locked()
            self._workers.append(compute)
            new_stats = _WorkerStats()
            # Seed the EMA so the *bias-corrected* m_hat equals the mesh
            # median. m_hat = m / (1 − β₁^step); solving for m with step=1
            # gives m = median × (1 − β₁).
            new_stats.m = median * (1.0 - self._beta1)
            new_stats.step = 1
            new_stats.last_latency = median
            self._stats.append(new_stats)
            return len(self._workers) - 1

    def remove_worker(self, idx: int) -> "Compute":
        """Evict a worker from the pool. Returns the evicted Compute."""
        with self._lock:
            if not (0 <= idx < len(self._workers)):
                raise IndexError(f"worker index {idx} out of range")
            if len(self._workers) == 1:
                raise ValueError(
                    "cannot remove the last worker; add a replacement first"
                )
            compute = self._workers.pop(idx)
            self._stats.pop(idx)
            return compute

    # ................................................................. warmup

    def warmup(
        self,
        *,
        probe_fn: Optional[Any] = None,
        rounds: int = 3,
        timeout: float = 10.0,
        eject_on_failure: bool = True,
        set_backpressure: bool = True,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Probe every worker before real traffic to calibrate priors.

        Runs ``rounds`` successful round-trips per worker against a cheap
        payload (``probe_fn``, defaults to an identity function). The
        observed latencies seed each worker's EMA, replacing the pessimistic
        ``initial_latency`` bootstrap.

        When ``eject_on_failure`` is True, workers that fail every probe
        within ``timeout`` seconds are removed from the pool before the
        method returns — they never see real traffic.

        When ``set_backpressure`` is True, the method updates
        ``backpressure_threshold`` to `1.5 × max(observed_worker_latency)`,
        biasing toward skipping an eval whenever the slow side can't
        keep up.

        Returns a report dict that callers can log / store:

        .. code-block:: python

            {
                "rounds": 3,
                "workers": [
                    {"idx": 0, "uri": "...", "ok": True,
                     "latency_mean": 0.31, "latency_p95": 0.42, "observed": [...]},
                    {"idx": 1, "uri": "...", "ok": False, "reason": "timeout"},
                ],
                "ejected": [1],
                "recommended_backpressure": 0.63,
                "applied_backpressure": True,
            }

        """
        import zakuro as zk

        if probe_fn is None:
            probe_fn = _identity

        # Wrap probe_fn as a zk.fn we can dispatch to a single worker.
        probe = zk.fn(probe_fn) if not hasattr(probe_fn, "_func") else probe_fn

        # Snapshot the list of (idx, compute) up-front so mutations during
        # the walk don't skip or double-count workers.
        with self._lock:
            snapshot: list[tuple[int, "Compute"]] = list(enumerate(self._workers))

        worker_reports: list[dict[str, Any]] = []
        # Track ORIGINAL indices; we translate to current positions right
        # before eviction (pool may have changed under us).
        failed_uris: list[str] = []

        for orig_idx, compute in snapshot:
            observed: list[float] = []
            err: Optional[str] = None
            started = time.perf_counter()
            for _ in range(rounds):
                remaining = timeout - (time.perf_counter() - started)
                if remaining <= 0:
                    err = err or "timeout"
                    break
                try:
                    t0 = time.perf_counter()
                    probe.to(compute)(_WARMUP_TOKEN)
                    observed.append(time.perf_counter() - t0)
                except Exception as exc:
                    err = repr(exc)
                    break

            if observed:
                observed.sort()
                mean = sum(observed) / len(observed)
                p95 = observed[min(len(observed) - 1, int(0.95 * len(observed)))]
                worker_reports.append(
                    {
                        "idx": orig_idx,
                        "uri": getattr(compute, "uri", None),
                        "ok": True,
                        "rounds_succeeded": len(observed),
                        "latency_mean": mean,
                        "latency_p95": p95,
                        "observed": list(observed),
                    }
                )
                # Seed the corresponding stats with the mean — look up by
                # identity since the index may have shifted.
                with self._lock:
                    for i, w in enumerate(self._workers):
                        if w is compute:
                            s = _WorkerStats()
                            s.m = mean
                            s.step = len(observed)
                            s.last_latency = observed[-1]
                            self._stats[i] = s
                            break
            else:
                worker_reports.append(
                    {
                        "idx": orig_idx,
                        "uri": getattr(compute, "uri", None),
                        "ok": False,
                        "reason": err or "no observations",
                    }
                )
                failed_uris.append(str(getattr(compute, "uri", orig_idx)))

        # Eject failed workers.
        ejected: list[int] = []
        if eject_on_failure and failed_uris:
            with self._lock:
                keep_workers = []
                keep_stats = []
                for i, w in enumerate(self._workers):
                    uri = str(getattr(w, "uri", i))
                    if uri in failed_uris and len(self._workers) - len(ejected) > 1:
                        ejected.append(i)
                        continue
                    keep_workers.append(w)
                    keep_stats.append(self._stats[i])
                self._workers = keep_workers
                self._stats = keep_stats

        # Recommend a backpressure threshold from the worst healthy worker's p95.
        healthy_p95 = [r["latency_p95"] for r in worker_reports if r["ok"]]
        recommended = max(healthy_p95) * 1.5 if healthy_p95 else None
        if set_backpressure and recommended is not None:
            self._backpressure = recommended

        report = {
            "rounds": rounds,
            "workers": worker_reports,
            "ejected": ejected,
            "recommended_backpressure": recommended,
            "applied_backpressure": set_backpressure and recommended is not None,
        }

        if verbose:
            self._print_warmup_report(report)

        return report

    @staticmethod
    def _print_warmup_report(report: dict[str, Any]) -> None:
        parts = []
        for w in report["workers"]:
            uri = w.get("uri") or f"worker-{w['idx']}"
            if w["ok"]:
                parts.append(f"{uri} (p95={w['latency_p95']:.3f}s)")
            else:
                parts.append(f"{uri}: EJECTED ({w['reason']})")
        summary = ", ".join(parts)
        bp = report["recommended_backpressure"]
        bp_str = f"{bp:.2f}s" if bp is not None else "<none>"
        print(f"[warmup] {summary}")
        print(f"[warmup] recommended backpressure: {bp_str}")

    def dispatch(
        self,
        fn: Any,  # zakuro.fn.Fn — avoids circular import
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Pick a worker, execute the function, record the latency.

        Any exception raised by the worker is propagated unchanged, after
        the worker's failure counter and queue depth are updated.
        """
        with self._lock:
            idx = self._pick_locked()
            self._stats[idx].queue += 1
        compute = self._workers[idx]
        t0 = time.perf_counter()
        try:
            fn.to(compute)
            result = fn._execute_single_compute(*args, **kwargs)  # type: ignore[attr-defined]
            return result
        except Exception:
            with self._lock:
                self._stats[idx].failures += 1
            raise
        finally:
            latency = time.perf_counter() - t0
            with self._lock:
                s = self._stats[idx]
                s.queue = max(0, s.queue - 1)
                self._update_ema(s, latency)

    # ........................................................... internals

    def _pick_locked(self) -> int:
        expected = self._expected_times_locked()
        if self._tau <= 0.0:
            # Argmin with stable tie-breaking — prefer the earlier worker.
            return min(range(len(expected)), key=lambda i: (expected[i], i))
        # Soft (probabilistic) allocation: logits = -time/τ.
        logits = [-t / self._tau for t in expected]
        m = max(logits)
        weights = [math.exp(l - m) for l in logits]
        total = sum(weights)
        r = random.random() * total
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if r <= acc:
                return i
        return len(weights) - 1  # numerical safety

    def _expected_times_locked(self) -> list[float]:
        out = []
        for s in self._stats:
            m = s.m_hat(self._beta1)
            lat = m if s.step > 0 else self._initial_latency
            out.append((s.queue + 1) * lat)
        return out

    def _best_expected_time(self) -> float:
        return min(self._expected_times_locked())

    def _mesh_median_latency_locked(self) -> float:
        """Median of the bias-corrected latency EMAs for observed workers.

        Falls back to ``initial_latency`` if no worker has any observations
        yet — matches the bootstrap assumption from the first call.
        """
        observed = [
            s.m_hat(self._beta1) for s in self._stats if s.step > 0
        ]
        if not observed:
            return self._initial_latency
        observed.sort()
        mid = len(observed) // 2
        if len(observed) % 2 == 1:
            return observed[mid]
        return 0.5 * (observed[mid - 1] + observed[mid])

    def _update_ema(self, s: _WorkerStats, latency: float) -> None:
        s.step += 1
        prev_m = s.m
        s.m = self._beta1 * s.m + (1.0 - self._beta1) * latency
        # Variance EMA uses the deviation from the *previous* EMA — matches the
        # on-line Welford-style update adopted by Adam-variant implementations.
        deviation = latency - prev_m
        s.v = self._beta2 * s.v + (1.0 - self._beta2) * (deviation * deviation)
        s.last_latency = latency

    # ........................................................ dunder utilities

    def __len__(self) -> int:
        return len(self._workers)

    def __repr__(self) -> str:
        return (
            f"AdaptiveCompute(n_workers={len(self._workers)}, "
            f"beta1={self._beta1}, beta2={self._beta2}, "
            f"tau={self._tau}, backpressure={self._backpressure}s)"
        )


__all__ = ["AdaptiveCompute"]
