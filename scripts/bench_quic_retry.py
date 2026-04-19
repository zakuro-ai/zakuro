"""Measured experiment: QUIC processor retry after worker bounce.

Starts a QUIC worker, dispatches a task (baseline), SIGKILLs + respawns
the worker on the same port, then dispatches again. Reports:

  - baseline dispatch latency
  - failure latency (the in-flight dispatch during the kill)
  - failover latency (fresh dispatch after respawn — exercises _reconnect)

Every number is observed from the running system.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import shutil

import httpx

import zakuro as zk


def _spawn_quic_worker(port: int, name: str) -> subprocess.Popen:
    binary = shutil.which("zakuro-worker")
    if binary is None:
        raise RuntimeError("zakuro-worker CLI not on PATH")
    proc = subprocess.Popen(
        [
            binary,
            "--transport", "quic",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--worker-name", name,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Poll HEALTH via QUIC until the worker responds.
    from zakuro.compute import Compute
    from zakuro.processors.base import ProcessorConfig
    from zakuro.processors.quic import QuicProcessor

    deadline = time.perf_counter() + 15.0
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"worker {name!r} exited early ({proc.returncode})")
        try:
            compute = Compute(uri=f"quic://127.0.0.1:{port}", verify=False)
            config = ProcessorConfig(scheme="quic", host="127.0.0.1", port=port)
            p = QuicProcessor(config, compute)
            p.connect()
            try:
                if p.ping():
                    return proc
            finally:
                p.disconnect()
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"worker {name!r} not ready within 15s")


def _identity(x: int) -> int:
    return x + 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4450)
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    results: dict = {"events": []}

    proc = _spawn_quic_worker(args.port, "retry-bench")
    compute = zk.Compute(uri=f"quic://127.0.0.1:{args.port}", verify=False)
    probe = zk.fn(_identity)

    try:
        print(f"worker up (pid={proc.pid})")

        # 1. Baseline dispatch.
        t0 = time.perf_counter()
        out = probe.to(compute)(41)
        baseline_ms = 1000 * (time.perf_counter() - t0)
        print(f"baseline dispatch: {out}  ({baseline_ms:.1f} ms)")
        results["events"].append({"kind": "baseline", "latency_ms": baseline_ms})

        # 2. Kill the worker.
        print("SIGKILL")
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        t_killed = time.perf_counter()

        # 3. Try a dispatch — expected to fail fast.
        try:
            t0 = time.perf_counter()
            probe.to(compute)(99)
            during_kill = {"kind": "during_kill", "ok": True,
                           "latency_ms": 1000 * (time.perf_counter() - t0)}
        except Exception as exc:
            during_kill = {
                "kind": "during_kill",
                "ok": False,
                "latency_ms": 1000 * (time.perf_counter() - t0),
                "error": repr(exc),
            }
        print(f"during-kill dispatch: {during_kill}")
        results["events"].append(during_kill)

        # 4. Respawn on the same port.
        print("respawning worker on same port...")
        proc2 = _spawn_quic_worker(args.port, "retry-bench-2")
        t_respawn = time.perf_counter()
        respawn_ms = 1000 * (t_respawn - t_killed)
        print(f"worker back up (pid={proc2.pid})  (respawn+health {respawn_ms:.0f} ms)")

        # 5. Fresh dispatch — needs a new Compute with verify=False (the old
        # Compute held a cached URI but the processor's connection pool is
        # stale). The retry path inside QuicProcessor tears down the dead
        # connection and dials a fresh one.
        t0 = time.perf_counter()
        out = probe.to(compute)(100)
        failover_ms = 1000 * (time.perf_counter() - t0)
        print(f"post-respawn dispatch: {out}  ({failover_ms:.1f} ms)")
        results["events"].append({"kind": "post_respawn", "latency_ms": failover_ms})

        proc2.terminate()
        proc2.wait()

        results["summary"] = {
            "baseline_ms": baseline_ms,
            "post_respawn_ms": failover_ms,
            "respawn_overhead_ms": respawn_ms,
        }
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()

    if args.log:
        Path(args.log).write_text(json.dumps(results, indent=2, default=float))
        print(f"log -> {args.log}")


if __name__ == "__main__":
    main()
