#!/usr/bin/env python3
"""
Two-node Tailscale scenario test.

Prerequisites:
  - docker compose -f docker/docker-compose.two-nodes.yml up -d
  - Both brokers healthy: curl localhost:9001/health && curl localhost:9002/health
  - Env: ZK0NODE01_ZAKURO_KEY and ZK0NODE02_ZAKURO_KEY (loaded from ~/.zakuro/env)

Scenarios:
  1. Both nodes broadcast a job with budget=5 credits (best-price routing)
  2. Each node forces self-execution (local worker, cost=0)
  3. Each node requests execution on the alien node (remote, billed)

After each scenario the script prints a full balance sheet for both users.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

# ── Credentials ──────────────────────────────────────────────────────────────
NODE1_KEY = os.environ.get("ZK0NODE01_ZAKURO_KEY") or os.environ.get("ZAKURO_API_KEY")
NODE2_KEY = os.environ.get("ZK0NODE02_ZAKURO_KEY")

NODE1_URL = os.environ.get("NODE1_URL", "http://localhost:9001")
NODE2_URL = os.environ.get("NODE2_URL", "http://localhost:9002")

if not NODE1_KEY:
    sys.exit("ERROR: ZK0NODE01_ZAKURO_KEY not set. Source ~/.zakuro/env first.")
if not NODE2_KEY:
    sys.exit("ERROR: ZK0NODE02_ZAKURO_KEY not set. Source ~/.zakuro/env first.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
    )


def get_balance(base_url: str, api_key: str) -> dict:
    """Return /me info (user_id + balance) for the given broker."""
    with _client(base_url, api_key) as c:
        r = c.get("/me")
        r.raise_for_status()
        return r.json()


def get_workers(base_url: str, api_key: str) -> list[dict]:
    with _client(base_url, api_key) as c:
        r = c.get("/workers")
        r.raise_for_status()
        return r.json().get("workers", [])


def execute_job(
    base_url: str,
    api_key: str,
    budget_credits: float | None = None,
    estimated_duration_secs: float = 1.0,
    remote_only: bool = False,
    strategy: str = "best_price",
) -> dict:
    """Submit a cloudpickle job to the broker and return billing metadata."""
    import cloudpickle
    import json

    def _workload():
        import time
        time.sleep(0.1)
        return {"status": "done", "node": os.uname().nodename if hasattr(os, "uname") else "unknown"}

    import os as _os
    payload = cloudpickle.dumps({"func": _workload, "args": (), "kwargs": {}})

    requirements: dict = {
        "cpus": 1.0,
        "memory_bytes": 256 * 1024 * 1024,
        "estimated_duration_secs": estimated_duration_secs,
        "strategy": strategy,
        "remote_only": remote_only,
    }
    if budget_credits is not None:
        requirements["budget_credits"] = budget_credits

    with _client(base_url, api_key) as c:
        r = c.post(
            "/execute",
            content=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Zakuro-Requirements": json.dumps(requirements),
            },
        )

        cost = float(r.headers.get("X-Zakuro-Cost", "0") or "0")
        remaining = float(r.headers.get("X-Zakuro-Credits-Remaining", "0") or "0")
        worker_id = r.headers.get("X-Zakuro-Worker", "?")

        if r.status_code == 402:
            return {"error": "INSUFFICIENT_CREDITS", "cost": 0.0, "remaining": remaining}
        if r.status_code == 503:
            return {"error": "NO_WORKERS", "cost": 0.0, "remaining": remaining}
        r.raise_for_status()

        result = cloudpickle.loads(r.content)
        return {
            "result": result,
            "cost": cost,
            "remaining": remaining,
            "worker_id": worker_id,
        }


def print_separator(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print('═' * 60)


def print_balance_sheet(label: str, n1: dict, n2: dict) -> None:
    print(f"\n  Balance sheet — {label}")
    print(f"  {'Node':<12} {'User':<18} {'Balance':>12}")
    print(f"  {'-'*12} {'-'*18} {'-'*12}")
    print(f"  {'Node 1':<12} {n1.get('user_id','?'):<18} {n1.get('balance', 0):>12.6f}")
    print(f"  {'Node 2':<12} {n2.get('user_id','?'):<18} {n2.get('balance', 0):>12.6f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print_separator("SETUP: Verify brokers are reachable")

    for label, url, key in [("Node 1", NODE1_URL, NODE1_KEY), ("Node 2", NODE2_URL, NODE2_KEY)]:
        try:
            r = httpx.get(f"{url}/health", timeout=5)
            r.raise_for_status()
            print(f"  {label} ({url}) — {r.json().get('status', 'ok')}")
        except Exception as e:
            sys.exit(f"  {label} unreachable: {e}")

    print_separator("SETUP: Worker discovery (wait for P2P sync)")
    time.sleep(3)  # brief pause for P2P discovery to propagate

    for label, url, key in [("Node 1", NODE1_URL, NODE1_KEY), ("Node 2", NODE2_URL, NODE2_KEY)]:
        workers = get_workers(url, key)
        local = [w for w in workers if w.get("is_local") or "127.0.0.1" in w.get("uri", "")]
        remote = [w for w in workers if not (w.get("is_local") or "127.0.0.1" in w.get("uri", ""))]
        print(f"  {label}: {len(workers)} workers total — {len(local)} local, {len(remote)} remote")
        for w in workers:
            origin = "LOCAL" if (w.get("is_local") or "127.0.0.1" in w.get("uri", "")) else "REMOTE"
            print(f"    [{origin}] {w.get('name','?')}  uri={w.get('uri','?')}  price={w.get('price_per_hour',0):.4f}/hr")

    # ─────────────────────────────────────────────────────────
    # Baseline balances
    # ─────────────────────────────────────────────────────────
    b1 = get_balance(NODE1_URL, NODE1_KEY)
    b2 = get_balance(NODE2_URL, NODE2_KEY)
    print_balance_sheet("BASELINE", b1, b2)

    # ─────────────────────────────────────────────────────────
    # SCENARIO 1 — Broadcast: both nodes submit a 5-credit budgeted job
    # ─────────────────────────────────────────────────────────
    print_separator("SCENARIO 1 — Broadcast (budget_credits=5, best_price routing)")
    print("  Each node submits a job with budget=5 credits.")
    print("  The broker picks the cheapest available worker.")
    print()

    for label, url, key in [("Node 1", NODE1_URL, NODE1_KEY), ("Node 2", NODE2_URL, NODE2_KEY)]:
        r = execute_job(url, key, budget_credits=5.0, estimated_duration_secs=1.0)
        if "error" in r:
            print(f"  {label}: FAILED ({r['error']})")
        else:
            print(f"  {label}: cost={r['cost']:.6f}  remaining={r['remaining']:.6f}  worker={r['worker_id']}")

    b1_after1 = get_balance(NODE1_URL, NODE1_KEY)
    b2_after1 = get_balance(NODE2_URL, NODE2_KEY)
    print_balance_sheet("AFTER SCENARIO 1", b1_after1, b2_after1)
    print(f"\n  Delta Node 1: {b1_after1['balance'] - b1['balance']:+.6f}")
    print(f"  Delta Node 2: {b2_after1['balance'] - b2['balance']:+.6f}")

    # ─────────────────────────────────────────────────────────
    # SCENARIO 2 — Self-assignment: force local worker only (cost = 0)
    # ─────────────────────────────────────────────────────────
    print_separator("SCENARIO 2 — Self-assignment (local worker, cost=0)")
    print("  Each node submits a job with remote_only=False.")
    print("  The broker picks its own local worker — free execution.")
    print()

    for label, url, key in [("Node 1", NODE1_URL, NODE1_KEY), ("Node 2", NODE2_URL, NODE2_KEY)]:
        r = execute_job(url, key, remote_only=False, strategy="best_price")
        if "error" in r:
            print(f"  {label}: FAILED ({r['error']})")
        else:
            print(f"  {label}: cost={r['cost']:.6f}  remaining={r['remaining']:.6f}  worker={r['worker_id']}")

    b1_after2 = get_balance(NODE1_URL, NODE1_KEY)
    b2_after2 = get_balance(NODE2_URL, NODE2_KEY)
    print_balance_sheet("AFTER SCENARIO 2", b1_after2, b2_after2)
    print(f"\n  Delta from scenario 1 — Node 1: {b1_after2['balance'] - b1_after1['balance']:+.6f}")
    print(f"  Delta from scenario 1 — Node 2: {b2_after2['balance'] - b2_after1['balance']:+.6f}")

    # ─────────────────────────────────────────────────────────
    # SCENARIO 3 — Alien execution: each node forces remote_only
    # ─────────────────────────────────────────────────────────
    print_separator("SCENARIO 3 — Alien node execution (remote_only=True)")
    print("  Each node submits a job with remote_only=True.")
    print("  The broker skips local workers and routes to the peer node via Tailscale.")
    print()

    for label, url, key in [("Node 1", NODE1_URL, NODE1_KEY), ("Node 2", NODE2_URL, NODE2_KEY)]:
        r = execute_job(url, key, budget_credits=5.0, remote_only=True, strategy="best_price")
        if "error" in r:
            print(f"  {label}: FAILED ({r['error']})  — peer workers may not be discovered yet")
        else:
            print(f"  {label}: cost={r['cost']:.6f}  remaining={r['remaining']:.6f}  worker={r['worker_id']}")

    b1_after3 = get_balance(NODE1_URL, NODE1_KEY)
    b2_after3 = get_balance(NODE2_URL, NODE2_KEY)
    print_balance_sheet("AFTER SCENARIO 3", b1_after3, b2_after3)
    print(f"\n  Delta from scenario 2 — Node 1: {b1_after3['balance'] - b1_after2['balance']:+.6f}")
    print(f"  Delta from scenario 2 — Node 2: {b2_after3['balance'] - b2_after2['balance']:+.6f}")

    # ─────────────────────────────────────────────────────────
    # Full summary
    # ─────────────────────────────────────────────────────────
    print_separator("FULL BALANCE SUMMARY")
    print(f"\n  {'Step':<30} {'Node 1':>14} {'Node 2':>14}")
    print(f"  {'-'*30} {'-'*14} {'-'*14}")
    rows = [
        ("Baseline",           b1,       b2),
        ("After S1 (broadcast)",  b1_after1, b2_after1),
        ("After S2 (self-assign)", b1_after2, b2_after2),
        ("After S3 (alien exec)",  b1_after3, b2_after3),
    ]
    for step, r1, r2 in rows:
        print(f"  {step:<30} {r1['balance']:>14.6f} {r2['balance']:>14.6f}")

    print(f"\n  {'Total spent Node 1':.<46} {b1['balance'] - b1_after3['balance']:>8.6f}")
    print(f"  {'Total spent Node 2':.<46} {b2['balance'] - b2_after3['balance']:>8.6f}")
    print()


if __name__ == "__main__":
    main()
