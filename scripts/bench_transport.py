"""Micro-benchmark comparing HTTP and QUIC transports.

Run:
    uv run python scripts/bench_transport.py --requests 2000 --concurrency 32
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import zakuro as zk


@zk.fn
def tiny(x: int) -> int:
    """Intentionally cheap — we're measuring transport overhead, not work."""
    return x + 1


def bench(transport: str, requests: int, concurrency: int) -> dict[str, float]:
    with zk.Worker.spawn(name=f"bench-{transport}", transport=transport) as worker:
        compute = worker.compute(verify=False)

        # Warm up: first call builds the connection.
        tiny.to(compute)(0)

        # The client Processor caches a connection per `Compute`.  To stress
        # concurrency we need per-thread Compute instances when transport=http
        # (httpx client is thread-safe) or one shared Compute + connection for
        # QUIC (the aioquic protocol serialises writes on the loop thread).
        latencies: list[float] = []
        lock = threading.Lock()

        def _one(i: int) -> None:
            t0 = time.perf_counter()
            tiny.to(compute)(i)
            t1 = time.perf_counter()
            with lock:
                latencies.append(t1 - t0)

        t_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(_one, i) for i in range(requests)]
            for f in as_completed(futs):
                f.result()
        t_end = time.perf_counter()

    elapsed = t_end - t_start
    return {
        "transport": transport,
        "requests": requests,
        "concurrency": concurrency,
        "rps": requests / elapsed,
        "p50_ms": statistics.median(latencies) * 1000,
        "p95_ms": statistics.quantiles(latencies, n=20)[18] * 1000,
        "p99_ms": statistics.quantiles(latencies, n=100)[98] * 1000,
    }


def _fmt(row: dict[str, float]) -> str:
    return (
        f"{row['transport']:6s} | "
        f"{int(row['requests']):>6} req | "
        f"c={int(row['concurrency']):>3} | "
        f"{row['rps']:>8.0f} rps | "
        f"p50={row['p50_ms']:>6.2f} ms | "
        f"p95={row['p95_ms']:>6.2f} ms | "
        f"p99={row['p99_ms']:>6.2f} ms"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument(
        "--transports",
        default="http,quic",
        help="Comma-separated list of transports to benchmark.",
    )
    args = parser.parse_args()

    print(
        f"{'tx':6s} | {'req':>10} | {'cc':>5} | {'rps':>8}"
        f" | {'p50':>10} | {'p95':>10} | {'p99':>10}"
    )
    print("-" * 88)
    for transport in args.transports.split(","):
        row = bench(transport.strip(), args.requests, args.concurrency)
        print(_fmt(row))


if __name__ == "__main__":
    main()
