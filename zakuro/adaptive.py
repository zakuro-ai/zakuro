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

import json
import math
import os
import random
import threading
import time
from pathlib import Path
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
    health_strikes:
        Number of consecutive failed health probes. Reset on a successful
        probe or a successful real dispatch.
    suspended:
        ``True`` when the worker has accumulated ``max_strikes`` health
        misses. Suspended workers don't receive traffic but are not ejected
        — a single successful probe un-suspends them.
    """

    __slots__ = (
        "m", "v", "step", "queue", "failures", "last_latency",
        "health_strikes", "suspended",
        # Drift-detection state (Phase 1.2).
        "m_slow", "drift_factor",
        # Bandwidth state (Phase 4.1). Seeded by warmup's large-payload probe.
        "bandwidth_bps",
    )

    def __init__(self) -> None:
        self.m: float = 0.0
        self.v: float = 0.0
        self.step: int = 0
        self.queue: int = 0
        self.failures: int = 0
        self.last_latency: Optional[float] = None
        self.health_strikes: int = 0
        self.suspended: bool = False
        # Longer-horizon EMA used as the "baseline" that drift is measured
        # against. 1.0 = healthy; > 1.0 = deprioritised proportionally.
        self.m_slow: float = 0.0
        self.drift_factor: float = 1.0
        # Bytes-per-second throughput estimate. ``None`` means "unknown,
        # treat as infinite" so bandwidth-unaware callers don't regress.
        self.bandwidth_bps: Optional[float] = None

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

    def m_slow_hat(self, beta_slow: float) -> float:
        """Bias-corrected slow-EMA baseline."""
        if self.step == 0:
            return 0.0
        return self.m_slow / (1.0 - beta_slow**self.step)

    def to_dict(self, beta1: float, beta2: float, beta_slow: float) -> dict[str, Any]:
        return {
            "step": self.step,
            "queue": self.queue,
            "failures": self.failures,
            "latency_ema": self.m_hat(beta1),
            "latency_var": self.v_hat(beta2),
            "latency_baseline": self.m_slow_hat(beta_slow),
            "drift_factor": self.drift_factor,
            "last_latency": self.last_latency,
            "health_strikes": self.health_strikes,
            "suspended": self.suspended,
            "bandwidth_bps": self.bandwidth_bps,
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
        beta_slow: float = 0.995,
        drift_threshold: float = 2.0,
        drift_penalty: float = 5.0,
        drift_recovery_threshold: float = 1.1,
        softmax_temperature: float = 0.0,
        initial_latency: float = 1.0,
        backpressure_threshold: float = 30.0,
        cost_coefficient: float = 0.0,
        local_region: Optional[str] = None,
        local_rack: Optional[str] = None,
        region_penalty: float = 1.10,
        rack_penalty: float = 1.03,
    ) -> None:
        self._workers: list["Compute"] = list(workers)
        if not self._workers:
            raise ValueError("AdaptiveCompute requires at least one worker.")
        if not (0.0 <= beta1 < 1.0) or not (0.0 <= beta2 < 1.0):
            raise ValueError("beta1, beta2 must be in [0, 1).")
        if not (0.0 <= beta_slow < 1.0):
            raise ValueError("beta_slow must be in [0, 1).")
        if drift_threshold <= 1.0:
            raise ValueError("drift_threshold must be > 1")
        if drift_recovery_threshold < 1.0:
            raise ValueError("drift_recovery_threshold must be ≥ 1")

        self._beta1 = beta1
        self._beta2 = beta2
        # Slower EMA used as "this worker's normal performance". Drift is
        # detected when the fast EMA rises significantly above this baseline.
        self._beta_slow = beta_slow
        # Cost weighting (Phase 4.3). 0 = ignore price entirely; positive
        # values scale each worker's expected time by (1 + coef × price).
        if cost_coefficient < 0:
            raise ValueError("cost_coefficient must be >= 0")
        self._cost_coef = float(cost_coefficient)
        # Topology preference (Phase 4.2). ``local_region=None`` falls back to
        # ``$ZAKURO_REGION`` (same for rack) so environments can set this
        # once per host and every AdaptiveCompute picks it up. Penalties of
        # 1.0 disable the preference entirely.
        if region_penalty < 1.0 or rack_penalty < 1.0:
            raise ValueError("region_penalty and rack_penalty must be >= 1.0")
        self._local_region = local_region or os.environ.get("ZAKURO_REGION") or None
        self._local_rack = local_rack or os.environ.get("ZAKURO_RACK") or None
        self._region_penalty = float(region_penalty)
        self._rack_penalty = float(rack_penalty)
        # `fast_ema > drift_threshold * baseline` → mark as drifted.
        self._drift_threshold = float(drift_threshold)
        # Multiplier applied to the drifted worker's expected time-to-serve.
        self._drift_penalty = float(drift_penalty)
        # `fast_ema < drift_recovery_threshold * baseline` → clear the drift.
        self._drift_recovery = float(drift_recovery_threshold)
        self._tau = float(softmax_temperature)
        self._initial_latency = float(initial_latency)
        self._backpressure = float(backpressure_threshold)

        self._stats = [_WorkerStats() for _ in self._workers]
        self._lock = threading.Lock()

        # Health probing lifecycle (Phase 1.1).
        self._probe_thread: Optional[threading.Thread] = None
        self._probe_stop: threading.Event = threading.Event()
        self._probe_interval: float = 5.0
        self._probe_timeout: float = 2.0
        self._max_strikes: int = 3

        # Decision log (Phase 6.1). Append one JSON object per dispatch with
        # the allocator's prediction and the observed outcome. Users can
        # replay this offline to audit decisions / debug weird routing.
        self._log_path: Optional[Path] = None
        self._log_lock: threading.Lock = threading.Lock()

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
            return [
                s.to_dict(self._beta1, self._beta2, self._beta_slow)
                for s in self._stats
            ]

    def is_backpressured(self) -> bool:
        """True when the best worker's expected time-to-serve exceeds the cap."""
        with self._lock:
            return self._best_expected_time() > self._backpressure

    def pick(self, payload_bytes: int = 0) -> int:
        """Return the index of the worker to dispatch to next.

        Argmin of expected time-to-complete when ``softmax_temperature == 0``;
        otherwise samples softmax-weighted over expected times.

        ``payload_bytes`` (optional): hint the size of the request body so
        the picker can factor `payload / bandwidth_bps` into each worker's
        expected time. Call-sites that handle very large state_dicts
        (sakura's HF callback, DDP async-eval) should pass this; small
        RPCs can leave it 0.
        """
        with self._lock:
            return self._pick_locked(payload_bytes=payload_bytes)

    # ........................................................ decision log

    def enable_decision_log(self, path: Optional[str] = None) -> Path:
        """Turn on per-dispatch logging to a JSONL file.

        Each dispatch appends one line:

            {"t": <unix>, "fn": "<fn_name>", "picked": <idx>,
             "expected_secs": <allocator's estimate>,
             "actual_secs": <observed latency>,
             "ok": <bool>, "queue_before": <int>,
             "drift_factor": <float>}

        Default path is ``$HOME/.zakuro/allocator.jsonl`` (or
        ``$ZAKURO_DECISION_LOG`` if set). Directory is created on demand.

        Call with ``path=None`` to disable.
        """
        if path is None:
            env = os.environ.get("ZAKURO_DECISION_LOG")
            if env:
                path = env
            else:
                home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or "/tmp"
                path = str(Path(home) / ".zakuro" / "allocator.jsonl")
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_path = log_path
        return log_path

    def disable_decision_log(self) -> None:
        self._log_path = None

    def _log_decision(self, row: dict[str, Any]) -> None:
        """Best-effort append — never raises, never blocks the dispatch path."""
        if self._log_path is None:
            return
        try:
            line = json.dumps(row, default=str) + "\n"
            with self._log_lock:
                with open(self._log_path, "a", encoding="utf-8") as fp:
                    fp.write(line)
        except Exception:
            # Observability must never break the hot path.
            pass

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
            # gives m = median × (1 − β₁). Do the same for the slow EMA so
            # drift detection has a sensible baseline from the first call.
            new_stats.m = median * (1.0 - self._beta1)
            new_stats.m_slow = median * (1.0 - self._beta_slow)
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
        bandwidth_probe_bytes: int = 1024 * 1024,
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

                # Bandwidth probe: one transfer of a bytes payload, measure
                # round-trip minus the latency floor to estimate the
                # worker's effective throughput. Bandwidth dominates over
                # raw latency once payloads cross a few MiB (see Phase 4.1
                # in PLAN.md) — the allocator mixes it into expected time.
                bandwidth_bps: Optional[float] = None
                if bandwidth_probe_bytes > 0:
                    big_payload = b"\x00" * bandwidth_probe_bytes
                    bw_samples: list[float] = []
                    for _ in range(2):
                        try:
                            t0 = time.perf_counter()
                            probe.to(compute)(big_payload)
                            bw_samples.append(time.perf_counter() - t0)
                        except Exception:
                            break
                    if bw_samples:
                        # Best-case: subtract the latency floor so we don't
                        # count fixed RTT overhead against throughput. Round-
                        # trip moves the bytes twice (client → worker, back);
                        # we count only the outbound leg for simplicity.
                        best = min(bw_samples)
                        transfer = max(best - mean, 1e-3)
                        bandwidth_bps = bandwidth_probe_bytes / transfer

                worker_reports.append(
                    {
                        "idx": orig_idx,
                        "uri": getattr(compute, "uri", None),
                        "ok": True,
                        "rounds_succeeded": len(observed),
                        "latency_mean": mean,
                        "latency_p95": p95,
                        "observed": list(observed),
                        "bandwidth_bps": bandwidth_bps,
                    }
                )
                # Seed the corresponding stats with the mean — look up by
                # identity since the index may have shifted.
                with self._lock:
                    for i, w in enumerate(self._workers):
                        if w is compute:
                            s = _WorkerStats()
                            step = len(observed)
                            # Seed the raw EMAs so that bias-corrected `m_hat`
                            # and `m_slow_hat` BOTH equal the observed mean at
                            # the current step count. Without this, the longer
                            # EMA has much stronger bias correction than the
                            # shorter one and the slow-baseline would read
                            # artificially large, masking drift or flagging
                            # spurious drift depending on timing.
                            s.m = mean * (1.0 - self._beta1**step)
                            s.m_slow = mean * (1.0 - self._beta_slow**step)
                            s.step = step
                            s.last_latency = observed[-1]
                            s.bandwidth_bps = bandwidth_bps
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

    # ......................................................... health probes

    def start_health_probes(
        self,
        *,
        interval: float = 5.0,
        timeout: float = 2.0,
        max_strikes: int = 3,
    ) -> None:
        """Start a background heartbeat loop.

        Every ``interval`` seconds the loop probes each worker's HEALTH
        opcode (QUIC) or ``/health`` endpoint (HTTP). A miss increments the
        worker's ``health_strikes`` counter; on ``max_strikes`` consecutive
        misses the worker is **suspended** — traffic routes around it but
        it stays in the pool. A successful probe resets the counter and
        un-suspends.

        Safe to call multiple times; only one probe thread runs per
        ``AdaptiveCompute`` instance.
        """
        if interval <= 0:
            raise ValueError("interval must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_strikes < 1:
            raise ValueError("max_strikes must be ≥ 1")

        self._probe_interval = float(interval)
        self._probe_timeout = float(timeout)
        self._max_strikes = int(max_strikes)

        if self._probe_thread is not None and self._probe_thread.is_alive():
            return

        self._probe_stop.clear()
        self._probe_thread = threading.Thread(
            target=self._probe_loop,
            name="zakuro-adaptive-health",
            daemon=True,
        )
        self._probe_thread.start()

    def stop_health_probes(self, timeout: float = 5.0) -> None:
        """Signal the probe thread to stop and wait for it.

        Idempotent; safe to call from any thread.
        """
        self._probe_stop.set()
        if self._probe_thread is not None:
            self._probe_thread.join(timeout=timeout)
        self._probe_thread = None

    def probe_once(self) -> list[dict[str, Any]]:
        """Run a single probe round synchronously. Returns per-worker results.

        Exposed mainly for tests and for users who prefer to drive the
        heartbeat loop from their own scheduler.
        """
        with self._lock:
            snapshot: list[tuple[int, "Compute"]] = list(enumerate(self._workers))
        results: list[dict[str, Any]] = []
        for idx, compute in snapshot:
            ok, err, latency = self._probe(compute)
            with self._lock:
                # The pool may have mutated; find the worker by identity.
                cur_idx = self._find_locked(compute)
                if cur_idx is None:
                    continue
                s = self._stats[cur_idx]
                if ok:
                    s.health_strikes = 0
                    if s.suspended:
                        s.suspended = False
                    # A good probe is also an observation: fold it into the EMA
                    # so that workers picking up after a transient stall are
                    # credited for being fast again.
                    if latency is not None:
                        self._update_ema(s, latency)
                else:
                    s.health_strikes += 1
                    if s.health_strikes >= self._max_strikes:
                        s.suspended = True
            results.append(
                {
                    "idx": idx,
                    "uri": getattr(compute, "uri", None),
                    "ok": ok,
                    "latency": latency,
                    "error": err,
                }
            )
        return results

    def _probe_loop(self) -> None:
        while not self._probe_stop.is_set():
            try:
                self.probe_once()
            except Exception:  # defensive — loop must never die on a user error
                pass
            # Use Event.wait so stop is immediate.
            self._probe_stop.wait(self._probe_interval)

    def _probe(self, compute: "Compute") -> tuple[bool, Optional[str], Optional[float]]:
        """Probe a single worker's health. Returns (ok, error_reason, latency_sec)."""
        # Pick the right probe per scheme; fall through to the generic
        # worker-runner info endpoint if we don't know the processor.
        scheme = getattr(compute, "scheme", "")
        t0 = time.perf_counter()
        try:
            if scheme == "quic":
                from zakuro.processors.base import ProcessorConfig
                from zakuro.processors.quic import QuicProcessor

                config = ProcessorConfig(
                    scheme="quic", host=compute.host, port=compute.port
                )
                processor = QuicProcessor(config, compute)
                processor.connect()
                try:
                    ok = processor.ping()
                finally:
                    processor.disconnect()
            else:
                # HTTP / zakuro:// — hit /health directly without cloudpickling.
                import httpx

                url = f"http://{compute.host}:{compute.port}/health"
                try:
                    r = httpx.get(url, timeout=self._probe_timeout)
                    ok = r.status_code == 200
                except httpx.HTTPError as exc:
                    return False, repr(exc), None
            return ok, None if ok else "health returned non-ok", time.perf_counter() - t0
        except Exception as exc:
            return False, repr(exc), None

    def _find_locked(self, compute: "Compute") -> Optional[int]:
        for i, w in enumerate(self._workers):
            if w is compute:
                return i
        return None

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
            expected = self._expected_times_locked()[idx]
            queue_before = self._stats[idx].queue
            drift_factor = self._stats[idx].drift_factor
        compute = self._workers[idx]
        fn_name = getattr(getattr(fn, "_func", fn), "__name__", "<fn>")
        t0 = time.perf_counter()
        ok = False
        error: Optional[str] = None
        try:
            fn.to(compute)
            result = fn._execute_single_compute(*args, **kwargs)  # type: ignore[attr-defined]
            ok = True
            return result
        except Exception as exc:
            error = repr(exc)
            with self._lock:
                self._stats[idx].failures += 1
            raise
        finally:
            latency = time.perf_counter() - t0
            with self._lock:
                s = self._stats[idx]
                s.queue = max(0, s.queue - 1)
                self._update_ema(s, latency)
            # Cheap, optional, never-raises observability hook.
            if self._log_path is not None:
                self._log_decision({
                    "t": time.time(),
                    "fn": fn_name,
                    "picked": idx,
                    "expected_secs": expected,
                    "actual_secs": latency,
                    "ok": ok,
                    "queue_before": queue_before,
                    "drift_factor": drift_factor,
                    "error": error,
                })

    # ........................................................... internals

    def _pick_locked(self, payload_bytes: int = 0) -> int:
        expected = self._expected_times_locked(payload_bytes=payload_bytes)
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

    def _expected_times_locked(
        self, payload_bytes: int = 0,
    ) -> list[float]:
        out = []
        for i, s in enumerate(self._stats):
            if s.suspended:
                # Suspended workers get sent to infinity so the picker routes
                # around them — but they stay in the pool, so a successful
                # probe can rehabilitate them with one flip of `suspended`.
                out.append(float("inf"))
                continue
            m = s.m_hat(self._beta1)
            lat = m if s.step > 0 else self._initial_latency
            # drift_factor softly demotes a drifted worker without ejecting:
            # it still gets some traffic (so the fast EMA can recover if the
            # underlying symptom clears) but its expected time is stretched.
            expected = (s.queue + 1) * lat * s.drift_factor
            # Bandwidth-aware: for large payloads, transfer time often
            # dominates over latency. Fold it in when we have an estimate.
            if payload_bytes > 0 and s.bandwidth_bps and s.bandwidth_bps > 0:
                expected += payload_bytes / s.bandwidth_bps
            # Price-aware: if the Compute has a price_per_hour hint and the
            # user opted into cost-aware routing (cost_coefficient > 0),
            # deprioritise more-expensive workers proportionally to
            # (coef × price). At coef=0 this is a no-op.
            if self._cost_coef > 0:
                price = getattr(self._workers[i], "price_per_hour", None)
                if price is not None and price > 0:
                    expected *= 1.0 + self._cost_coef * price
            # Topology-aware: softly demote cross-region (and, secondarily,
            # cross-rack) workers. Small multipliers so a faster remote
            # worker still wins on latency — this only breaks near-ties.
            if self._local_region is not None:
                w = self._workers[i]
                w_region = getattr(w, "region", None)
                if w_region is not None and w_region != self._local_region:
                    expected *= self._region_penalty
                elif w_region == self._local_region and self._local_rack is not None:
                    w_rack = getattr(w, "rack", None)
                    if w_rack is not None and w_rack != self._local_rack:
                        expected *= self._rack_penalty
            out.append(expected)
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
        s.m_slow = self._beta_slow * s.m_slow + (1.0 - self._beta_slow) * latency
        s.last_latency = latency
        # Drift detection: a worker whose *fast* EMA has climbed past a
        # multiple of its own *slow* baseline is deprioritised. Only kicks in
        # after a minimum observation count so we don't trip on a cold start.
        if s.step >= 8:
            fast = s.m_hat(self._beta1)
            baseline = s.m_slow_hat(self._beta_slow)
            if baseline > 0:
                ratio = fast / baseline
                if ratio >= self._drift_threshold:
                    s.drift_factor = self._drift_penalty
                elif ratio <= self._drift_recovery and s.drift_factor > 1.0:
                    s.drift_factor = 1.0

    # ........................................................ dunder utilities

    def __len__(self) -> int:
        return len(self._workers)

    def __repr__(self) -> str:
        return (
            f"AdaptiveCompute(n_workers={len(self._workers)}, "
            f"beta1={self._beta1}, beta2={self._beta2}, "
            f"tau={self._tau}, backpressure={self._backpressure}s)"
        )

    def __del__(self) -> None:
        # Best-effort cleanup of the probe thread when the allocator is
        # garbage-collected. Users should call stop_health_probes() explicitly
        # in long-running processes.
        try:
            self.stop_health_probes(timeout=0.5)
        except Exception:
            pass


__all__ = ["AdaptiveCompute"]
