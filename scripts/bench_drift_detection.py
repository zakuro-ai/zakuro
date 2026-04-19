"""Measured drift-injection experiment.

Baseline two workers against a cheap task, then flip one of them into a
`_slow_task` for a window of time so that every dispatch it serves takes
~10× longer than normal. Watch:

  * how quickly the drift detector raises ``drift_factor``;
  * how the pick distribution shifts while drifted;
  * how the detector releases the worker once latency returns to baseline.

All numbers are measured from the running allocator; nothing is simulated
after the injection flip.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import zakuro as zk


def _fast_task() -> int:
    """~0.01 ms of CPU work — used as the baseline dispatch."""
    x = 0
    for i in range(10_000):
        x = (x + i) & 0xFFFFF
    return x


def _slow_task() -> int:
    """Much slower version — half a second of busy-wait. The experiment
    toggles between the two to simulate a worker whose throughput has
    dropped (e.g. it got a noisy neighbour, GPU thermal throttle, network
    contention). Uses time.sleep so the slowdown dominates over any
    fixed RPC overhead."""
    import time as _time
    _time.sleep(0.25)
    return 0


def _stage(
    label: str,
    fn: zk.Fn,
    adaptive: zk.AdaptiveCompute,
    slow_fn: zk.Fn,
    slow_idx: int,
    duration: float,
    rate: float,
) -> dict[str, Any]:
    """Run dispatches for ``duration`` at ``rate``. For dispatches that
    the allocator routes to ``slow_idx``, use ``slow_fn`` instead of ``fn``
    — that's the "injection": as if that worker itself was slower.
    """
    interval = 1.0 / max(rate, 0.1)
    picks = [0 for _ in adaptive.workers]
    t0 = time.perf_counter()
    first_drift_t: float | None = None
    cleared_drift_t: float | None = None
    injection_active = slow_idx is not None
    next_at = t0
    while time.perf_counter() - t0 < duration:
        now = time.perf_counter()
        if now >= next_at:
            idx = adaptive.pick()
            if injection_active and idx == slow_idx:
                slow_fn.to(adaptive)()
            else:
                fn.to(adaptive)()
            picks[idx] += 1
            next_at += interval

        stats = adaptive.stats()
        if (
            injection_active
            and first_drift_t is None
            and stats[slow_idx]["drift_factor"] > 1.0
        ):
            first_drift_t = now - t0
        if (
            not injection_active
            and cleared_drift_t is None
            and all(s["drift_factor"] == 1.0 for s in stats)
        ):
            cleared_drift_t = now - t0
        time.sleep(0.005)

    return {
        "label": label,
        "duration": duration,
        "picks": picks,
        "first_drift_secs": first_drift_t,
        "cleared_drift_secs": cleared_drift_t,
        "final_stats": adaptive.stats(),
    }


def _print_stage(stage: dict[str, Any]) -> None:
    print(f"\n--- {stage['label']} ({stage['duration']:.1f}s) ---")
    for i, n in enumerate(stage["picks"]):
        s = stage["final_stats"][i]
        fast = s["latency_ema"] * 1000
        slow = s["latency_baseline"] * 1000
        ratio = fast / slow if slow > 0 else float("nan")
        print(
            f"  worker {i}: picks={n:>4}  "
            f"fast_ema={fast:6.1f}ms  baseline={slow:6.1f}ms  "
            f"ratio={ratio:4.2f}  drift_factor={s['drift_factor']}"
        )
    if stage.get("first_drift_secs") is not None:
        print(f"  drift detected at t+{stage['first_drift_secs']:.2f}s")
    if stage.get("cleared_drift_secs") is not None:
        print(f"  drift cleared at t+{stage['cleared_drift_secs']:.2f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup-rounds", type=int, default=4)
    parser.add_argument("--baseline-secs", type=float, default=5.0)
    parser.add_argument("--injection-secs", type=float, default=10.0)
    parser.add_argument("--recovery-secs", type=float, default=10.0)
    parser.add_argument("--rate", type=float, default=20.0,
                        help="Dispatches per second.")
    parser.add_argument("--drift-threshold", type=float, default=1.5)
    parser.add_argument("--drift-penalty", type=float, default=5.0)
    parser.add_argument("--beta1", type=float, default=0.7)
    parser.add_argument("--beta-slow", type=float, default=0.97)
    parser.add_argument(
        "--softmax-temperature",
        type=float,
        default=0.05,
        help="Non-zero keeps both workers utilised so drift detection has "
             "observations to work with. Set to 0 for pure greedy.",
    )
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    workers = [zk.Worker.spawn(name=f"dr-{i}") for i in range(2)]
    print(f"spawned: {[w.uri for w in workers]}")

    adaptive = zk.AdaptiveCompute(
        workers=[w.compute(verify=False) for w in workers],
        beta1=args.beta1,
        beta_slow=args.beta_slow,
        drift_threshold=args.drift_threshold,
        drift_penalty=args.drift_penalty,
        softmax_temperature=args.softmax_temperature,
    )
    adaptive.warmup(rounds=args.warmup_rounds, verbose=False)
    # Background heartbeats feed successful probes into the EMA. Without
    # them, a drifted worker can never recover because traffic stops — no
    # observations, so the fast EMA stays stuck above the recovery
    # threshold. With them, the probes let a truly-healthy worker
    # re-qualify for traffic without a manual reset.
    adaptive.start_health_probes(interval=0.5, timeout=0.5, max_strikes=6)

    fast = zk.fn(_fast_task)
    slow = zk.fn(_slow_task)

    report: dict[str, Any] = {
        "params": vars(args),
        "stages": [],
    }

    try:
        # 1. Baseline — both workers run the fast task.
        s1 = _stage("baseline", fast, adaptive, slow, slow_idx=None,
                    duration=args.baseline_secs, rate=args.rate)
        _print_stage(s1)
        report["stages"].append(s1)

        # 2. Inject drift into worker 0.
        s2 = _stage("drift injected (worker 0 slow)",
                    fast, adaptive, slow, slow_idx=0,
                    duration=args.injection_secs, rate=args.rate)
        _print_stage(s2)
        report["stages"].append(s2)

        # 3. Recover — drop the injection.
        s3 = _stage("recovery (injection stopped)",
                    fast, adaptive, slow, slow_idx=None,
                    duration=args.recovery_secs, rate=args.rate)
        _print_stage(s3)
        report["stages"].append(s3)
    finally:
        adaptive.stop_health_probes()
        for w in workers:
            w.stop()

    if args.log:
        Path(args.log).write_text(json.dumps(report, indent=2, default=float))
        print(f"\nlog → {args.log}")


if __name__ == "__main__":
    main()
