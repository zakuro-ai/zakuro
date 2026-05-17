"""Measured-only mesh experiments on a real 2-worker cluster.

Every number this script prints is observed, not simulated. Run:

    uv run python scripts/bench_mesh_adaptation.py --n-workers 2 --dispatch 100

The script spawns N zakuro-worker subprocesses (via ``zk.Worker.spawn``),
builds an ``AdaptiveCompute`` over them, and walks through four checkpoints,
printing the real measured behaviour:

    1. warmup            → per-worker p95 + ejection decision + BP recommend
    2. dispatch N tasks  → allocation histogram across workers
    3. remove_worker(0)  → dispatch N more → verify all traffic shifts
    4. add_worker back   → dispatch N more → verify traffic rebalances

The logs go both to stdout and to ``--log results.json`` for later analysis.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

import zakuro as zk


def _slow_task(n: int = 50_000) -> int:
    """A deterministic CPU-bound chunk — ~1 ms on a modern core.

    Kept small and pure so cloudpickle dispatches cheaply; the goal is to
    observe routing behaviour, not to measure compute throughput.
    """
    acc = 0
    for i in range(n):
        acc = (acc + i * 7919) % (10**9 + 7)
    return acc


def _dispatch_round(
    fn: Any,
    adaptive: zk.AdaptiveCompute,
    count: int,
    label: str,
) -> dict[str, Any]:
    """Send ``count`` tasks through the allocator and return a measured summary."""
    latencies: list[float] = []

    # We don't know in advance which worker gets picked — AdaptiveCompute
    # records stats via dispatch(), so we can read picks from stats() delta.
    before_steps = [s["step"] for s in adaptive.stats()]
    t0 = time.perf_counter()
    for _ in range(count):
        a0 = time.perf_counter()
        fn.to(adaptive)()
        latencies.append(time.perf_counter() - a0)
    total = time.perf_counter() - t0
    after_steps = [s["step"] for s in adaptive.stats()]
    picks_delta = [a - b for a, b in zip(after_steps, before_steps, strict=False)]

    summary = {
        "label": label,
        "count": count,
        "wall_secs": total,
        "per_worker_picks": picks_delta,
        "latency_median_ms": 1000.0 * statistics.median(latencies) if latencies else 0.0,
        "latency_p95_ms": (
            1000.0 * sorted(latencies)[int(0.95 * len(latencies))]
            if len(latencies) >= 5 else 0.0
        ),
        "worker_stats": adaptive.stats(),
    }
    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"\n--- {summary['label']} ---")
    print(
        f"  {summary['count']} dispatches in {summary['wall_secs']:.2f}s  "
        f"(median {summary['latency_median_ms']:.1f}ms, "
        f"p95 {summary['latency_p95_ms']:.1f}ms)"
    )
    print("  pick distribution:")
    for i, n in enumerate(summary["per_worker_picks"]):
        pct = 100 * n / summary["count"] if summary["count"] else 0
        s = summary["worker_stats"][i]
        print(
            f"    worker {i}: {n:>4} picks ({pct:>5.1f}%)  "
            f"ema={s['latency_ema']*1000:.1f}ms  queue={s['queue']}  "
            f"failures={s['failures']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-workers", type=int, default=2)
    parser.add_argument(
        "--dispatch",
        type=int,
        default=100,
        help="Dispatches per checkpoint.",
    )
    parser.add_argument(
        "--transport",
        choices=("http", "quic"),
        default="http",
        help="Transport for spawned workers. HTTP is more portable; QUIC "
             "needs the [worker] extra installed.",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--softmax-temperature",
        type=float,
        default=0.0,
        help="0 = greedy argmin; >0 enables probabilistic routing.",
    )
    parser.add_argument("--log", default=None, help="Write JSON results to this path.")
    args = parser.parse_args()

    random.seed(args.seed)
    results: dict[str, Any] = {
        "n_workers": args.n_workers,
        "dispatch": args.dispatch,
        "transport": args.transport,
        "stages": [],
    }

    # -------------------- spawn workers --------------------
    workers = [
        zk.Worker.spawn(name=f"bench-{i}", transport=args.transport)
        for i in range(args.n_workers)
    ]
    print(f"Spawned {len(workers)} workers: {[w.uri for w in workers]}")

    adaptive = zk.AdaptiveCompute(
        workers=[w.compute(verify=False) for w in workers],
        beta1=0.8,
        beta2=0.99,
        initial_latency=0.5,
        backpressure_threshold=999.0,
        softmax_temperature=args.softmax_temperature,
    )
    results["softmax_temperature"] = args.softmax_temperature

    # A zk.fn dispatched across the allocator.
    slow = zk.fn(_slow_task)

    try:
        # -------------------- 1. warmup --------------------
        print("\n=== 1. warmup ===")
        warmup_report = adaptive.warmup(rounds=3, timeout=5.0, verbose=True)
        results["warmup"] = warmup_report
        print(f"  (allocator.backpressure_threshold = "
              f"{adaptive.backpressure_threshold:.3f}s)")

        # -------------------- 2. dispatch N tasks --------------------
        s1 = _dispatch_round(slow, adaptive, args.dispatch, "after warmup")
        _print_summary(s1)
        results["stages"].append(s1)

        # -------------------- 3. remove worker 0, dispatch again --------------
        print("\n=== 3. remove worker 0 (node departure) ===")
        removed = adaptive.remove_worker(0)
        print(f"  removed: {removed.uri}")
        print(f"  remaining workers: {[w.uri for w in adaptive.workers]}")
        s2 = _dispatch_round(slow, adaptive, args.dispatch, "after removal")
        _print_summary(s2)
        results["stages"].append(s2)

        # -------------------- 4. readmit, dispatch again ---------------------
        print("\n=== 4. readmit the worker (node admission) ===")
        new_idx = adaptive.add_worker(workers[0].compute(verify=False))
        print(f"  readmitted at idx {new_idx}, "
              f"seeded ema = {adaptive.stats()[new_idx]['latency_ema']*1000:.1f}ms")
        s3 = _dispatch_round(slow, adaptive, args.dispatch, "after readmission")
        _print_summary(s3)
        results["stages"].append(s3)

    finally:
        for w in workers:
            w.stop()

    if args.log:
        Path(args.log).write_text(json.dumps(results, indent=2, default=float))
        print(f"\nresults written to {args.log}")


if __name__ == "__main__":
    main()
