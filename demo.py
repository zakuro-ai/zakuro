#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["zakuro-ai[worker]>=0.2.24"]
# ///
"""Zakuro in one command: kill a worker mid-job and lose nothing.

    uv run demo.py           # from a fresh clone; installs zakuro-ai from PyPI

Starts three Zakuro workers on this machine, then counts the primes below
100,000,000 by sending 200 chunks of the range to them, one call at a time.
A third of the way in, one worker is killed outright (no warning, no clean
shutdown). Zakuro's allocator notices, resends the interrupted call to a
live worker, and stops routing to the dead one. The demo then checks the
final count against the known answer, 5,761,455.

No GPU, no Docker, no account: three local worker processes on loopback.
"""
from __future__ import annotations

import sys
import threading
import time

import zakuro as zk

LIMIT, CHUNKS = 100_000_000, 200
KNOWN_ANSWER = 5_761_455  # number of primes below 10^8
KILL_AT_CHUNK = CHUNKS // 3
REPO = "https://github.com/zakuro-ai/zakuro"


def count_primes(lo: int, hi: int) -> int:
    """Primes in [lo, hi), by a segmented sieve. Runs on whichever worker Zakuro picks."""
    import math

    root = math.isqrt(hi - 1) + 1
    base = bytearray([1]) * root
    base[0:2] = b"\x00\x00"
    for p in range(2, math.isqrt(root - 1) + 1):
        if base[p]:
            base[p * p::p] = bytes(len(range(p * p, root, p)))
    seg = bytearray([1]) * (hi - lo)
    for p in (i for i in range(2, root) if base[i]):
        start = max(p * p, (lo + p - 1) // p * p)
        seg[start - lo::p] = bytes(len(range(start, hi, p)))
    for n in range(lo, min(2, hi)):
        seg[n - lo] = 0
    return sum(seg)


def bar(n: int, total: int, width: int = 24) -> str:
    filled = round(width * n / total) if total else 0
    return "#" * filled + "." * (width - filled)


def main() -> int:
    print("\nStarting 3 Zakuro workers on this machine ...")
    workers = [zk.Worker.spawn(name=f"worker-{i}") for i in range(3)]
    for i, w in enumerate(workers):
        print(f"  worker-{i}  pid {w.pid:<7} {w.uri}")

    pool = zk.AdaptiveCompute(
        workers=[w.compute(verify=False) for w in workers],
        softmax_temperature=0.02,  # spread load instead of always picking the single fastest
        max_dispatch_retries=2,    # a failed call is resent to a different worker
    )
    pool.warmup(rounds=2, verbose=False)
    pool.start_health_probes(interval=0.25, max_strikes=2)
    task = zk.fn(count_primes)

    victim = 1
    step = LIMIT // CHUNKS
    total = 0
    failed = 0
    t0 = time.perf_counter()
    print(f"\nCounting primes below {LIMIT:,} in {CHUNKS} chunks; "
          f"worker-{victim} gets killed at chunk {KILL_AT_CHUNK}.\n")
    try:
        for i in range(CHUNKS):
            if i == KILL_AT_CHUNK:
                # Kill it from another thread a moment after the next call goes out,
                # so it can die with work in flight -- the worst case.
                threading.Timer(0.005, workers[victim]._proc.kill).start()
                print(f"  !! killing worker-{victim} (pid {workers[victim].pid}) mid-job\n")
            try:
                total += task.to(pool)(i * step, (i + 1) * step)
            except Exception as exc:  # would mean the call was lost
                failed += 1
                print(f"  chunk {i} FAILED: {exc!r}")
            if (i + 1) % 25 == 0:
                per = "   ".join(
                    f"w{j} {s['step']:>3}{' (down)' if s.get('suspended') else ''}"
                    for j, s in enumerate(pool.stats()))
                print(f"  {i + 1:>3}/{CHUNKS} [{bar(i + 1, CHUNKS)}]   calls sent: {per}")
        elapsed = time.perf_counter() - t0
        stats = pool.stats()
    finally:
        pool.stop_health_probes()
        for w in workers:
            if w.is_running:
                w.stop()

    dead = stats[victim]
    calls_after_kill_note = (
        f"{dead['failures']} call(s) hit the dead worker and were resent elsewhere"
        if dead.get("failures") else "the next call was already routed elsewhere"
    )
    ok = total == KNOWN_ANSWER and failed == 0
    print(f"\n  primes below {LIMIT:,}: {total:,}  (known answer {KNOWN_ANSWER:,}) "
          f"{'-- correct' if total == KNOWN_ANSWER else '-- WRONG'}")
    print(f"  {CHUNKS - failed}/{CHUNKS} calls succeeded, {failed} lost; "
          f"{calls_after_kill_note}")
    print(f"  worker-{victim} marked {'suspended' if dead.get('suspended') else 'down'} "
          f"by the health probe; finished in {elapsed:.1f} s\n")

    print("The Zakuro part of demo.py:\n")
    print("    pool = zk.AdaptiveCompute(workers=[w.compute() for w in workers],")
    print("                              max_dispatch_retries=2)")
    print("    total += zk.fn(count_primes).to(pool)(lo, hi)   # runs on a worker\n")
    print("To spread work across machines: https://docs.zakuro-ai.com/guides/choosing-a-transport")
    print(f"\nIf this was useful, a star on {REPO} helps others find it,")
    print("and it's the easiest way to follow what we ship next.\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
