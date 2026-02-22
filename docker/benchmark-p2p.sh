#!/bin/bash
# Multi-Node P2P Broker Benchmark Script
#
# Tests broker scalability with 2, 5, and 10 nodes.
# Measures throughput (RPS), latency, and scalability factor.

set -e

# Configuration
BENCH_REQUESTS=${BENCH_REQUESTS:-10000}
BENCH_CONCURRENCY=${BENCH_CONCURRENCY:-50}
RESULTS_DIR="benchmark-results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${CYAN}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║${NC}         ${BOLD}Zakuro P2P Broker Multi-Node Benchmark${NC}           ${CYAN}║${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}Configuration:${NC}"
echo -e "  Requests per test: ${GREEN}${BENCH_REQUESTS}${NC}"
echo -e "  Concurrency:       ${GREEN}${BENCH_CONCURRENCY}${NC}"
echo -e "  Results directory: ${CYAN}${RESULTS_DIR}${NC}"
echo ""

# Create results directory
mkdir -p "${RESULTS_DIR}"

# Function to clean up
cleanup() {
    echo ""
    echo -e "${YELLOW}Cleaning up...${NC}"
    docker compose -f docker/docker-compose.bench.yml down -v 2>/dev/null || true
}
trap cleanup EXIT

# Function to wait for services
wait_for_services() {
    local nodes=$1
    echo -e "${YELLOW}Waiting for $nodes broker(s) to be healthy...${NC}"

    local max_wait=120
    local waited=0

    while [ $waited -lt $max_wait ]; do
        local healthy=$(docker compose -f docker/docker-compose.bench.yml ps broker 2>/dev/null | grep -c "healthy" || echo "0")

        if [ "$healthy" -eq "$nodes" ]; then
            echo -e "${GREEN}✓ All $nodes broker(s) are healthy${NC}"
            return 0
        fi

        echo -ne "\r  Waiting... ($waited/${max_wait}s, $healthy/$nodes ready)  "
        sleep 2
        waited=$((waited + 2))
    done

    echo -e "\n${RED}✗ Timeout waiting for brokers${NC}"
    return 1
}

# Function to seed credits
seed_credits() {
    echo -e "${YELLOW}Seeding credits for bench-user...${NC}"

    docker compose -f docker/docker-compose.bench.yml exec -T postgres psql -U zakuro -d zakuro_bench <<EOF
-- Ensure user exists and has credits
INSERT INTO users (zakuro_user_id, email, credits_balance)
VALUES ('bench-user', 'bench@zakuro.local', 10000.00)
ON CONFLICT (zakuro_user_id) DO UPDATE
SET credits_balance = 10000.00;
EOF

    echo -e "${GREEN}✓ Credits seeded${NC}"
}

# Function to get worker count
get_worker_count() {
    local broker_port=$1
    curl -sf "http://localhost:${broker_port}/workers" 2>/dev/null | \
        python3 -c "import sys, json; print(json.load(sys.stdin).get('total', 0))" 2>/dev/null || echo "0"
}

# Function to run benchmark on a specific broker
run_single_benchmark() {
    local nodes=$1
    local iteration=$2
    local broker_port=$3
    local output_file="${RESULTS_DIR}/bench_${nodes}nodes_iter${iteration}_${TIMESTAMP}.txt"

    echo -e "\n${BOLD}Running benchmark (iteration $iteration/$nodes brokers, port $broker_port)...${NC}"

    # Check worker count
    local workers=$(get_worker_count $broker_port)
    echo -e "  Workers available: ${GREEN}${workers}${NC}"

    if [ "$workers" -eq "0" ]; then
        echo -e "${RED}  ✗ No workers available!${NC}"
        echo "NO_WORKERS" > "$output_file"
        return 1
    fi

    # Run benchmark
    docker run --rm --network docker_bench-net \
        $(docker build -q -f ../../zak-zc/docker/Dockerfile ../../zak-zc) \
        zc bench "http://broker:9000" \
        -c "${BENCH_CONCURRENCY}" \
        -n "${BENCH_REQUESTS}" \
        --user "bench-user-${iteration}" \
        2>&1 | tee "$output_file"

    echo -e "${GREEN}✓ Benchmark completed${NC}"
    echo -e "  Results saved to: ${CYAN}${output_file}${NC}"
}

# Function to extract RPS from benchmark output
extract_rps() {
    local file=$1
    grep "Throughput:" "$file" 2>/dev/null | awk '{print $2}' | head -1 || echo "0"
}

# Function to extract average latency
extract_avg_latency() {
    local file=$1
    grep "Avg:" "$file" 2>/dev/null | awk '{print $2}' | head -1 || echo "0"
}

# Function to extract p95 latency
extract_p95() {
    local file=$1
    grep "p95:" "$file" 2>/dev/null | awk '{print $2}' | head -1 || echo "0"
}

# Function to extract p99 latency
extract_p99() {
    local file=$1
    grep "p99:" "$file" 2>/dev/null | awk '{print $2}' | head -1 || echo "0"
}

# Function to test with N nodes
test_nodes() {
    local nodes=$1

    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Testing with ${nodes} broker node(s)${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""

    # Clean previous deployment
    cleanup
    sleep 2

    # Set P2P mode based on node count
    if [ "$nodes" -eq 1 ]; then
        export ZAKURO_P2P=false
        export ZAKURO_PEERS="worker:3960"
    else
        export ZAKURO_P2P=true
        # Build worker peer list: worker-1:3960,worker-2:3960,...
        # Format: "host:port" without http:// prefix
        ZAKURO_PEERS=""
        for i in $(seq 1 $nodes); do
            if [ -n "$ZAKURO_PEERS" ]; then
                ZAKURO_PEERS="${ZAKURO_PEERS},"
            fi
            ZAKURO_PEERS="${ZAKURO_PEERS}worker-${i}:3960"
        done
        export ZAKURO_PEERS
    fi

    echo -e "${YELLOW}Worker discovery: ${ZAKURO_PEERS}${NC}"

    # Start PostgreSQL first
    echo -e "${YELLOW}Starting PostgreSQL...${NC}"
    docker compose -f docker/docker-compose.bench.yml up -d postgres
    sleep 5

    # Start brokers
    echo -e "${YELLOW}Starting ${nodes} broker(s)...${NC}"
    docker compose -f docker/docker-compose.bench.yml up -d --scale broker=${nodes} --scale worker=${nodes} broker worker

    # Wait for services
    if ! wait_for_services $nodes; then
        echo -e "${RED}✗ Failed to start ${nodes} node(s)${NC}"
        return 1
    fi

    # Seed credits
    seed_credits

    # Give services time to discover each other
    echo -e "${YELLOW}Waiting for P2P discovery...${NC}"
    sleep 10

    # Get first broker port (always 9000 in bridge network)
    local broker_port=9000

    # Run benchmark
    run_single_benchmark $nodes 1 $broker_port

    # Extract results
    local output_file="${RESULTS_DIR}/bench_${nodes}nodes_iter1_${TIMESTAMP}.txt"
    local rps=$(extract_rps "$output_file")
    local avg_lat=$(extract_avg_latency "$output_file")
    local p95=$(extract_p95 "$output_file")
    local p99=$(extract_p99 "$output_file")

    # Store results
    echo "$nodes,$rps,$avg_lat,$p95,$p99" >> "${RESULTS_DIR}/summary_${TIMESTAMP}.csv"

    echo ""
    echo -e "${GREEN}✓ ${nodes}-node test completed${NC}"
    echo -e "  RPS:        ${BOLD}${rps}${NC}"
    echo -e "  Avg Latency: ${avg_lat} ms"
    echo -e "  P95 Latency: ${p95} ms"
    echo -e "  P99 Latency: ${p99} ms"
}

# Main execution
main() {
    # Create summary CSV header
    echo "nodes,rps,avg_latency_ms,p95_latency_ms,p99_latency_ms" > "${RESULTS_DIR}/summary_${TIMESTAMP}.csv"

    # Test with 1 node (baseline)
    test_nodes 1

    # Test with 2 nodes
    test_nodes 2

    # Test with 5 nodes
    test_nodes 5

    # Test with 10 nodes
    test_nodes 10

    # Generate summary report
    echo ""
    echo -e "${CYAN}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}                    ${BOLD}Benchmark Summary${NC}                      ${CYAN}║${NC}"
    echo -e "${CYAN}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    echo -e "${BOLD}Results:${NC}"
    echo ""
    printf "  %-8s %-12s %-12s %-12s %-12s %-15s\n" "Nodes" "RPS" "Avg (ms)" "P95 (ms)" "P99 (ms)" "Scaling Factor"
    echo "  ────────────────────────────────────────────────────────────────────────────"

    # Read baseline (1 node)
    local baseline_rps=$(awk -F',' 'NR==2 {print $2}' "${RESULTS_DIR}/summary_${TIMESTAMP}.csv")

    # Print each row with scaling factor
    awk -F',' -v baseline="$baseline_rps" '
    NR>1 {
        nodes=$1
        rps=$2
        avg=$3
        p95=$4
        p99=$5
        scaling=(baseline>0 ? rps/(baseline*nodes) : 0)
        printf "  %-8s %-12.1f %-12.2f %-12.2f %-12.2f %-15.2f\n", nodes, rps, avg, p95, p99, scaling
    }
    ' "${RESULTS_DIR}/summary_${TIMESTAMP}.csv"

    echo ""
    echo -e "${BOLD}Scalability Analysis:${NC}"
    echo ""
    echo -e "  • ${BOLD}Perfect linear scaling${NC} = 1.00 (RPS grows proportionally with nodes)"
    echo -e "  • ${BOLD}Good scaling${NC}         = 0.80-1.00 (minor overhead)"
    echo -e "  • ${BOLD}Fair scaling${NC}         = 0.50-0.80 (moderate overhead)"
    echo -e "  • ${BOLD}Poor scaling${NC}         < 0.50 (significant bottlenecks)"
    echo ""
    echo -e "${CYAN}Full results saved to: ${RESULTS_DIR}/${NC}"
    echo ""
}

# Run main
main
