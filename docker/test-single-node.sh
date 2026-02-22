#!/bin/bash
# Simple single-node benchmark test
# Verifies infrastructure before running full multi-node tests

set -e

cd "$(dirname "$0")/.."

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║         Zakuro P2P Broker - Single Node Test                 ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

# Clean up
echo "→ Cleaning up previous containers..."
docker compose -f docker/docker-compose.bench.yml down -v 2>/dev/null || true
sleep 2

# Start PostgreSQL
echo "→ Starting PostgreSQL..."
docker compose -f docker/docker-compose.bench.yml up -d postgres

# Wait for PostgreSQL
echo "→ Waiting for PostgreSQL..."
for i in {1..30}; do
    if docker compose -f docker/docker-compose.bench.yml exec -T postgres pg_isready -U zakuro >/dev/null 2>&1; then
        echo "✓ PostgreSQL is ready"
        break
    fi
    sleep 1
done

# Start 1 broker and 1 worker
echo "→ Starting broker and worker..."
export ZAKURO_P2P=false  # Disable P2P for single node
export ZAKURO_PEERS="worker:3960"  # Point broker to worker service (no http://)
docker compose -f docker/docker-compose.bench.yml up -d --scale broker=1 --scale worker=1 broker worker

# Wait for broker
echo "→ Waiting for broker to be healthy..."
for i in {1..60}; do
    if docker compose -f docker/docker-compose.bench.yml ps broker 2>&1 | grep -q "healthy"; then
        echo "✓ Broker is healthy"
        break
    fi
    sleep 1
done

# Seed credits
echo "→ Seeding credits..."
docker compose -f docker/docker-compose.bench.yml exec -T postgres psql -U zakuro -d zakuro <<EOF
INSERT INTO users (zakuro_user_id, email, credits_balance)
VALUES ('bench-user', 'bench@zakuro.local', 10000.00)
ON CONFLICT (zakuro_user_id) DO UPDATE
SET credits_balance = 10000.00;
EOF
echo "✓ Credits seeded"

# Wait for worker registration
echo "→ Waiting for worker registration..."
sleep 5

# Check worker count
echo "→ Checking workers..."
WORKERS=$(docker compose -f docker/docker-compose.bench.yml exec -T broker curl -sf http://localhost:9000/workers 2>/dev/null | python3 -c "import sys, json; print(json.load(sys.stdin).get('total', 0))" 2>/dev/null || echo "0")
echo "  Workers available: $WORKERS"

if [ "$WORKERS" -eq "0" ]; then
    echo "✗ No workers available! Check logs:"
    docker compose -f docker/docker-compose.bench.yml logs broker worker
    exit 1
fi

# Run quick benchmark
echo ""
echo "→ Running benchmark (1000 requests, concurrency 10)..."
echo ""

# Build broker image for benchmark client
BROKER_IMAGE=$(docker build -q -f ../zak-zc/docker/Dockerfile ../zak-zc)

# Run benchmark
docker run --rm --network docker_bench-net \
    "$BROKER_IMAGE" \
    zc bench http://broker:9000 \
    -c 10 \
    -n 1000 \
    --user bench-user

echo ""
echo "✓ Single-node test completed successfully!"
echo ""
echo "Next steps:"
echo "  1. Review the results above"
echo "  2. If successful, run full multi-node benchmark:"
echo "     cd docker && ./run-benchmarks.py"
echo "  3. Clean up: docker compose -f docker/docker-compose.bench.yml down -v"
echo ""
