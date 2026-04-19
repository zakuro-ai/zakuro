"""Measured experiment: health probe detecting a killed worker.

Spawns two workers, starts `AdaptiveCompute.start_health_probes`, lets the
background thread see both as healthy, then kills worker 0 with SIGKILL
and measures how long the allocator takes to stop sending traffic there.

Every number is observed.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path

import zakuro as zk


def _tiny_work() -> int:
    return 42


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe-interval", type=float, default=0.5)
    parser.add_argument("--probe-timeout", type=float, default=0.3)
    parser.add_argument("--max-strikes", type=int, default=2)
    parser.add_argument("--dispatch-rate", type=float, default=10.0,
                        help="Dispatches per second in the foreground loop.")
    parser.add_argument("--duration", type=float, default=10.0,
                        help="Run for this many seconds total.")
    parser.add_argument("--kill-at", type=float, default=3.0,
                        help="Kill worker 0 at this offset (seconds).")
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    workers = [zk.Worker.spawn(name=f"hd-{i}") for i in range(2)]
    print(f"spawned: {[w.uri for w in workers]}  pids={[w.pid for w in workers]}")

    adaptive = zk.AdaptiveCompute(
        workers=[w.compute(verify=False) for w in workers],
        beta1=0.8,
    )
    adaptive.warmup(rounds=2, verbose=False)
    adaptive.start_health_probes(
        interval=args.probe_interval,
        timeout=args.probe_timeout,
        max_strikes=args.max_strikes,
    )

    tiny = zk.fn(_tiny_work)

    events: list[dict] = []
    suspend_time: float | None = None
    killed = False
    start = time.perf_counter()
    next_dispatch = start
    interval = 1.0 / max(args.dispatch_rate, 0.1)

    try:
        while True:
            now = time.perf_counter() - start
            if now >= args.duration:
                break
            if not killed and now >= args.kill_at:
                workers[0]._proc.send_signal(signal.SIGKILL)
                events.append({"t": now, "kind": "kill", "worker": 0})
                print(f"  [{now:5.2f}s] SIGKILL worker 0 (pid {workers[0].pid})")
                killed = True

            # Dispatch at the target rate.
            if time.perf_counter() >= next_dispatch:
                try:
                    tiny.to(adaptive)()
                except Exception:
                    pass
                next_dispatch += interval

            # Detect the suspension.
            stats = adaptive.stats()
            if suspend_time is None and killed and stats[0]["suspended"]:
                suspend_time = now
                events.append({"t": now, "kind": "suspend", "worker": 0})
                print(f"  [{now:5.2f}s] worker 0 suspended by health probe")

            time.sleep(0.02)

        # Summary.
        final_stats = adaptive.stats()
        total_picks = [s["step"] for s in final_stats]
        print("\n--- summary ---")
        print(f"worker 0: step={final_stats[0]['step']}  suspended={final_stats[0]['suspended']}  "
              f"strikes={final_stats[0]['health_strikes']}")
        print(f"worker 1: step={final_stats[1]['step']}  suspended={final_stats[1]['suspended']}  "
              f"strikes={final_stats[1]['health_strikes']}")
        if suspend_time is not None:
            detect = suspend_time - args.kill_at
            print(f"detection latency: {detect*1000:.0f} ms "
                  f"(kill → suspend, max_strikes={args.max_strikes}, "
                  f"probe_interval={args.probe_interval}s)")
        else:
            print("worker 0 never suspended (tune probe params / increase duration)")

        if args.log:
            Path(args.log).write_text(
                json.dumps(
                    {
                        "params": vars(args),
                        "events": events,
                        "final_stats": final_stats,
                        "total_picks_per_worker": total_picks,
                        "suspend_latency_secs": (
                            suspend_time - args.kill_at if suspend_time else None
                        ),
                    },
                    indent=2,
                    default=float,
                )
            )
            print(f"log → {args.log}")
    finally:
        adaptive.stop_health_probes()
        for w in workers:
            if w.is_running:
                w.stop()


if __name__ == "__main__":
    main()
