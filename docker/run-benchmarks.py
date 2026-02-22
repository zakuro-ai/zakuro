#!/usr/bin/env python3
"""
Multi-Node P2P Broker Benchmark Orchestrator

Runs performance benchmarks on 1, 2, 5, and 10 broker nodes.
Measures throughput, latency, and scalability factor.
"""

import subprocess
import time
import json
import csv
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple

# Configuration
BENCH_REQUESTS = int(os.environ.get("BENCH_REQUESTS", "10000"))
BENCH_CONCURRENCY = int(os.environ.get("BENCH_CONCURRENCY", "50"))
RESULTS_DIR = Path("benchmark-results")
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# Colors
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    CYAN = '\033[0;36m'
    BOLD = '\033[1m'
    NC = '\033[0m'

def print_header(text: str):
    """Print a formatted header"""
    print(f"\n{Colors.CYAN}{'═' * 70}{Colors.NC}")
    print(f"{Colors.BOLD}  {text}{Colors.NC}")
    print(f"{Colors.CYAN}{'═' * 70}{Colors.NC}\n")

def run_command(cmd: List[str], capture=False) -> Tuple[int, str]:
    """Run a shell command"""
    try:
        if capture:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return result.returncode, result.stdout
        else:
            result = subprocess.run(cmd, check=False)
            return result.returncode, ""
    except Exception as e:
        print(f"{Colors.RED}Error running command: {e}{Colors.NC}")
        return 1, str(e)

def cleanup():
    """Clean up Docker resources"""
    print(f"{Colors.YELLOW}Cleaning up Docker resources...{Colors.NC}")
    run_command(["docker", "compose", "-f", "docker/docker-compose.bench.yml", "down", "-v"])
    time.sleep(2)

def wait_for_postgres():
    """Wait for PostgreSQL to be ready"""
    print(f"{Colors.YELLOW}Waiting for PostgreSQL...{Colors.NC}")
    max_wait = 30
    for i in range(max_wait):
        code, output = run_command([
            "docker", "compose", "-f", "docker/docker-compose.bench.yml",
            "exec", "-T", "postgres", "pg_isready", "-U", "zakuro"
        ], capture=True)
        if code == 0:
            print(f"{Colors.GREEN}✓ PostgreSQL is ready{Colors.NC}")
            return True
        time.sleep(1)
    print(f"{Colors.RED}✗ PostgreSQL timeout{Colors.NC}")
    return False

def seed_credits():
    """Seed credits for test users"""
    print(f"{Colors.YELLOW}Seeding credits...{Colors.NC}")

    sql = """
    INSERT INTO users (zakuro_user_id, email, credits_balance)
    VALUES ('bench-user', 'bench@zakuro.local', 100000.00)
    ON CONFLICT (zakuro_user_id) DO UPDATE
    SET credits_balance = 100000.00;
    """

    code, _ = run_command([
        "docker", "compose", "-f", "docker/docker-compose.bench.yml",
        "exec", "-T", "postgres",
        "psql", "-U", "zakuro", "-d", "zakuro",
        "-c", sql
    ], capture=True)

    if code == 0:
        print(f"{Colors.GREEN}✓ Credits seeded{Colors.NC}")
        return True
    else:
        print(f"{Colors.RED}✗ Failed to seed credits{Colors.NC}")
        return False

def get_worker_count() -> int:
    """Get number of workers from broker"""
    try:
        code, output = run_command([
            "docker", "compose", "-f", "docker/docker-compose.bench.yml",
            "exec", "-T", "broker",
            "curl", "-sf", "http://localhost:9000/workers"
        ], capture=True)
        if code == 0:
            data = json.loads(output)
            return data.get('total', 0)
    except:
        pass
    return 0

def run_benchmark(nodes: int) -> Dict:
    """Run benchmark for N nodes"""
    print_header(f"Testing with {nodes} broker node(s)")

    # Clean up
    cleanup()

    # Set P2P mode
    os.environ['ZAKURO_P2P'] = 'true' if nodes > 1 else 'false'

    # Set ZAKURO_PEERS for worker discovery
    # Docker Compose with --scale creates worker-1, worker-2, etc.
    # Format: "host:port" without http:// prefix
    if nodes == 1:
        os.environ['ZAKURO_PEERS'] = 'worker:3960'
    else:
        worker_urls = [f'worker-{i}:3960' for i in range(1, nodes + 1)]
        os.environ['ZAKURO_PEERS'] = ','.join(worker_urls)

    print(f"{Colors.YELLOW}Worker discovery: {os.environ['ZAKURO_PEERS']}{Colors.NC}")

    # Start PostgreSQL
    print(f"{Colors.YELLOW}Starting PostgreSQL...{Colors.NC}")
    run_command(["docker", "compose", "-f", "docker/docker-compose.bench.yml",  "up", "-d", "postgres"])
    if not wait_for_postgres():
        return {"error": "PostgreSQL failed"}

    # Start brokers and workers
    print(f"{Colors.YELLOW}Starting {nodes} broker(s) and worker(s)...{Colors.NC}")
    run_command([
        "docker", "compose", "-f", "docker/docker-compose.bench.yml",
        "up", "-d", "--scale", f"broker={nodes}", "--scale", f"worker={nodes}",
        "broker", "worker"
    ])

    # Wait for brokers to be healthy
    print(f"{Colors.YELLOW}Waiting for brokers to be healthy...{Colors.NC}")
    max_wait = 60
    for i in range(max_wait):
        code, output = run_command([
            "docker", "compose", "-f", "docker/docker-compose.bench.yml",
            "ps", "broker"
        ], capture=True)
        if "healthy" in output and output.count("healthy") >= nodes:
            print(f"{Colors.GREEN}✓ All {nodes} broker(s) are healthy{Colors.NC}")
            break
        time.sleep(2)
    else:
        print(f"{Colors.RED}✗ Timeout waiting for brokers{Colors.NC}")
        return {"error": "Broker timeout"}

    # Seed credits
    if not seed_credits():
        return {"error": "Credit seeding failed"}

    # Wait for worker discovery
    print(f"{Colors.YELLOW}Waiting for worker discovery...{Colors.NC}")
    time.sleep(10)

    # Check workers
    workers = get_worker_count()
    print(f"  Workers available: {Colors.GREEN}{workers}{Colors.NC}")

    if workers == 0:
        print(f"{Colors.RED}✗ No workers available!{Colors.NC}")
        return {"error": "No workers"}

    # Run benchmark
    print(f"\n{Colors.BOLD}Running benchmark...{Colors.NC}")
    output_file = RESULTS_DIR / f"bench_{nodes}nodes_{TIMESTAMP}.txt"

    with open(output_file, "w") as f:
        result = subprocess.run([
            "docker", "run", "--rm", "--network", "docker_bench-net",
            "-e", "DATABASE_URL=postgresql://zakuro_broker:broker_secret_change_me@postgres:5432/zakuro",
            "$(docker build -q -f ../../zak-zc/docker/Dockerfile ../../zak-zc)",
            "zc", "bench", "http://broker:9000",
            "-c", str(BENCH_CONCURRENCY),
            "-n", str(BENCH_REQUESTS),
            "--user", "bench-user"
        ], stdout=f, stderr=subprocess.STDOUT, text=True, shell=True)

    # Extract results
    with open(output_file) as f:
        content = f.read()

    print(content)

    # Parse results
    rps = 0.0
    avg_latency = 0.0
    p95 = 0.0
    p99 = 0.0

    for line in content.split('\n'):
        if 'Throughput:' in line:
            try:
                rps = float(line.split()[1])
            except:
                pass
        elif 'Avg:' in line:
            try:
                avg_latency = float(line.split()[1])
            except:
                pass
        elif 'p95:' in line:
            try:
                p95 = float(line.split()[1])
            except:
                pass
        elif 'p99:' in line:
            try:
                p99 = float(line.split()[1])
            except:
                pass

    print(f"{Colors.GREEN}✓ {nodes}-node test completed{Colors.NC}")
    print(f"  RPS:         {Colors.BOLD}{rps:.1f}{Colors.NC}")
    print(f"  Avg Latency: {avg_latency:.2f} ms")
    print(f"  P95 Latency: {p95:.2f} ms")
    print(f"  P99 Latency: {p99:.2f} ms")

    return {
        "nodes": nodes,
        "rps": rps,
        "avg_latency_ms": avg_latency,
        "p95_latency_ms": p95,
        "p99_latency_ms": p99,
        "workers": workers
    }

def generate_report(results: List[Dict]):
    """Generate summary report"""
    print_header("Benchmark Summary")

    # Calculate baseline
    baseline_rps = next((r["rps"] for r in results if r["nodes"] == 1), 0)

    # Print table
    print(f"\n{Colors.BOLD}Results:{Colors.NC}\n")
    print(f"  {'Nodes':<8} {'RPS':<12} {'Avg (ms)':<12} {'P95 (ms)':<12} {'P99 (ms)':<12} {'Scaling':<15}")
    print(f"  {'─' * 75}")

    for result in results:
        nodes = result["nodes"]
        rps = result["rps"]
        scaling_factor = (rps / (baseline_rps * nodes)) if baseline_rps > 0 else 0

        print(f"  {nodes:<8} {rps:<12.1f} {result['avg_latency_ms']:<12.2f} "
              f"{result['p95_latency_ms']:<12.2f} {result['p99_latency_ms']:<12.2f} "
              f"{scaling_factor:<15.3f}")

    # Scalability analysis
    print(f"\n{Colors.BOLD}Scalability Analysis:{Colors.NC}\n")
    print(f"  • {Colors.BOLD}Perfect linear scaling{Colors.NC} = 1.00 (RPS grows proportionally with nodes)")
    print(f"  • {Colors.BOLD}Good scaling{Colors.NC}         = 0.80-1.00 (minor overhead)")
    print(f"  • {Colors.BOLD}Fair scaling{Colors.NC}         = 0.50-0.80 (moderate overhead)")
    print(f"  • {Colors.BOLD}Poor scaling{Colors.NC}         < 0.50 (significant bottlenecks)")

    # Save CSV
    csv_file = RESULTS_DIR / f"summary_{TIMESTAMP}.csv"
    with open(csv_file, "w", newline='') as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    print(f"\n{Colors.CYAN}Full results saved to: {RESULTS_DIR}/{Colors.NC}\n")

def main():
    """Main execution"""
    print_header("Zakuro P2P Broker Multi-Node Benchmark")

    print(f"{Colors.BOLD}Configuration:{Colors.NC}")
    print(f"  Requests per test: {Colors.GREEN}{BENCH_REQUESTS}{Colors.NC}")
    print(f"  Concurrency:       {Colors.GREEN}{BENCH_CONCURRENCY}{Colors.NC}")
    print(f"  Results directory: {Colors.CYAN}{RESULTS_DIR}{Colors.NC}\n")

    # Create results directory
    RESULTS_DIR.mkdir(exist_ok=True)

    # Change to zakuro directory
    os.chdir(Path(__file__).parent.parent)

    # Run benchmarks
    results = []
    for nodes in [1, 2, 5, 10]:
        result = run_benchmark(nodes)
        if "error" not in result:
            results.append(result)
        else:
            print(f"{Colors.RED}✗ Benchmark failed for {nodes} nodes: {result['error']}{Colors.NC}")

        # Small delay between tests
        time.sleep(5)

    # Cleanup
    cleanup()

    # Generate report
    if results:
        generate_report(results)
    else:
        print(f"{Colors.RED}✗ No successful benchmarks{Colors.NC}")
        return 1

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Interrupted by user{Colors.NC}")
        cleanup()
        sys.exit(130)
    except Exception as e:
        print(f"{Colors.RED}Error: {e}{Colors.NC}")
        import traceback
        traceback.print_exc()
        cleanup()
        sys.exit(1)
