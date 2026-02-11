#!/usr/bin/env python3
"""P2P mesh demo - sends real cloudpickle compute tasks through a broker.

Usage:
    python mesh-demo.py [BROKER_URL] [USER_ID]

Examples:
    python mesh-demo.py http://node1-broker:9000 node1-user
    python mesh-demo.py http://localhost:9001 node1-user
    python mesh-demo.py http://node2-broker:9000 node2-user
"""

import cloudpickle
import json
import sys
import time
import requests


BROKER_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9001"
USER_ID = sys.argv[2] if len(sys.argv) > 2 else "node1-user"


def execute(func, *args, strategy="best_price", **kwargs):
    """Send a cloudpickle-serialized function to the broker for execution."""
    payload = cloudpickle.dumps({"func": func, "args": args, "kwargs": kwargs})
    resp = requests.post(
        f"{BROKER_URL}/execute",
        data=payload,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Zakuro-User": USER_ID,
            "X-Zakuro-Requirements": json.dumps({
                "strategy": strategy,
                "estimated_duration_secs": 1.0,
            }),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return None, resp.status_code, 0

    result = cloudpickle.loads(resp.content)
    cost = resp.headers.get("X-Zakuro-Cost", "?")
    worker = resp.headers.get("X-Zakuro-Worker", "?")
    duration = resp.headers.get("X-Zakuro-Duration-Ms", "?")
    return result, cost, duration


# ---- Demo tasks ----

def fibonacci(n):
    """Recursive fibonacci - CPU-bound."""
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)


def matrix_work(size=200):
    """CPU-intensive matrix-like operations."""
    total = 0
    for i in range(size):
        for j in range(size):
            total += (i * j) % 17
    return total


def hello_mesh():
    """Simple function to verify connectivity."""
    import socket
    return f"Hello from {socket.gethostname()}!"


# ---- Main ----

def main():
    print(f"\n{'='*60}")
    print(f"  Zakuro P2P Mesh Demo")
    print(f"  Broker:  {BROKER_URL}")
    print(f"  User:    {USER_ID}")
    print(f"{'='*60}\n")

    # Check broker health
    try:
        health = requests.get(f"{BROKER_URL}/health", timeout=5)
        if health.status_code != 200:
            print(f"  Broker not healthy: {health.status_code}")
            sys.exit(1)
    except requests.ConnectionError:
        print(f"  Cannot connect to broker at {BROKER_URL}")
        sys.exit(1)

    # Show available workers
    workers = requests.get(f"{BROKER_URL}/workers", timeout=5).json()
    print(f"  Workers available: {workers['total']}")
    for w in workers.get("workers", []):
        print(f"    - {w['name']:25} type={w['worker_type']:10} "
              f"cpu=${w['price_per_cpu_sec']:.4f}/s  status={w['status']}")
    print()

    # Check credits
    creds = requests.get(f"{BROKER_URL}/credits/{USER_ID}", timeout=5).json()
    print(f"  Credits before: {creds.get('balance', 0):.4f}")
    print()

    # Run tasks with different strategies
    print(f"  {'Strategy':22} {'Task':12} {'Result':12} {'Cost':12} {'Time':10}")
    print(f"  {'-'*22} {'-'*12} {'-'*12} {'-'*12} {'-'*10}")

    strategies = ["best_price", "best_latency", "round_robin", "best_availability"]

    for strategy in strategies:
        result, cost, duration = execute(fibonacci, 30, strategy=strategy)
        if result is not None:
            print(f"  {strategy:22} fib(30)      {result:<12} {cost:>12} {duration:>8}ms")
        else:
            print(f"  {strategy:22} fib(30)      FAILED       status={cost}")

    print()

    # Run hello_mesh to show which host processed it
    for strategy in ["best_price", "round_robin"]:
        result, cost, duration = execute(hello_mesh, strategy=strategy)
        if result is not None:
            print(f"  {strategy:22} hello_mesh   {result}")

    print()

    # Run matrix_work
    result, cost, duration = execute(matrix_work, 150, strategy="best_price")
    if result is not None:
        print(f"  {'best_price':22} matrix(150)  {result:<12} {cost:>12} {duration:>8}ms")

    # Check credits after
    print()
    creds = requests.get(f"{BROKER_URL}/credits/{USER_ID}", timeout=5).json()
    print(f"  Credits after:  {creds.get('balance', 0):.4f}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
