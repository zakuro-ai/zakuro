"""One-stop perf benchmark. Runs the key scripts end-to-end and prints a
consolidated summary so users can verify every measured claim in PLAN.md
on their own hardware.

Every sub-benchmark uses real spawned workers — nothing is simulated.
Expected wall-clock on a modern Mac: ~60–90 s total.

    uv run python scripts/bench_all.py
    uv run python scripts/bench_all.py --log /tmp/zakuro-bench-$(date +%s).json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent


def _run_bench(args: list[str], label: str) -> dict[str, Any]:
    """Run a sub-benchmark, capture stdout, tag its rows as observations."""
    print(f"\n{'='*72}\n{label}\n{'='*72}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - t0
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return {
        "label": label,
        "argv": args,
        "returncode": proc.returncode,
        "wall_secs": elapsed,
        "stdout_tail": proc.stdout.splitlines()[-20:],
    }


def _fmt_row(row: dict[str, Any], width: int = 46) -> str:
    ok = "✓" if row["returncode"] == 0 else "✗"
    return f"  {ok} {row['label']:<{width}}  {row['wall_secs']:>6.1f} s"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--log",
        default=None,
        help="Write aggregated JSON results to this path.",
    )
    p.add_argument(
        "--skip",
        default="",
        help="Comma-separated sub-bench labels to skip (substring match).",
    )
    args = p.parse_args()
    skip = [s.strip().lower() for s in args.skip.split(",") if s.strip()]

    def should_run(label: str) -> bool:
        return not any(s in label.lower() for s in skip)

    all_results: list[dict[str, Any]] = []

    if should_run("mesh"):
        all_results.append(_run_bench(
            [str(SCRIPTS / "bench_mesh_adaptation.py"),
             "--n-workers", "2", "--dispatch", "50"],
            "mesh adaptation (warmup → dispatch → remove → readmit)",
        ))

    if should_run("health"):
        all_results.append(_run_bench(
            [str(SCRIPTS / "bench_health_detection.py"),
             "--duration", "4", "--kill-at", "1.5",
             "--probe-interval", "0.1", "--probe-timeout", "0.1",
             "--max-strikes", "1"],
            "health detection (SIGKILL → suspend latency)",
        ))

    if should_run("drift"):
        all_results.append(_run_bench(
            [str(SCRIPTS / "bench_drift_detection.py"),
             "--baseline-secs", "2", "--injection-secs", "6",
             "--recovery-secs", "2", "--rate", "15",
             "--drift-threshold", "1.5", "--softmax-temperature", "0.05"],
            "drift detection (induced slowdown → soft demote)",
        ))

    if should_run("quic"):
        all_results.append(_run_bench(
            [str(SCRIPTS / "bench_quic_retry.py"), "--port", "4476"],
            "QUIC retry (worker bounce → fast detection)",
        ))

    # ---------- consolidated summary ----------
    print(f"\n{'='*72}\nsummary\n{'='*72}")
    for row in all_results:
        print(_fmt_row(row))

    failures = [r for r in all_results if r["returncode"] != 0]
    if failures:
        print(f"\n❌ {len(failures)} sub-benchmark(s) failed. See above.")
    else:
        print(f"\n✅ all {len(all_results)} sub-benchmarks passed.")

    if args.log:
        Path(args.log).write_text(
            json.dumps({"ran_at": time.time(), "results": all_results},
                       indent=2, default=str)
        )
        print(f"log → {args.log}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
