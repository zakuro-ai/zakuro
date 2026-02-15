#!/bin/bash
# Demo script for Zakuro P2P Compute Broker
#
# This script demonstrates the broker flow using curl commands.
# Run the broker first: zc broker
# Run a worker: task worker:dev

set -e

BROKER_URL="${BROKER_URL:-http://localhost:9001}"
WORKER_URL="${WORKER_URL:-http://localhost:3960}"
USER_ID="${USER:-demo-user}"

echo "========================================"
echo "  Zakuro P2P Compute Broker Demo"
echo "========================================"
echo ""
echo "Broker URL: $BROKER_URL"
echo "Worker URL: $WORKER_URL"
echo "User ID:    $USER_ID"
echo ""

# 1. Health check
echo "1. Checking broker health..."
curl -s "$BROKER_URL/health" | python3 -m json.tool
echo ""

# 2. Register worker
echo "2. Registering worker..."
curl -s -X POST "$BROKER_URL/workers" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo-worker",
    "uri": "'"$WORKER_URL"'",
    "worker_type": "zakuro",
    "resources": {
      "cpus_total": 4.0,
      "cpus_available": 4.0,
      "memory_total": 8589934592,
      "memory_available": 8589934592,
      "gpus_total": 0,
      "gpus_available": 0
    },
    "pricing": {
      "cpu_price": 0.001,
      "memory_price": 0.0001,
      "gpu_price": 0.01,
      "min_charge": 0.0001
    }
  }' | python3 -m json.tool
echo ""

# 3. List workers
echo "3. Listing workers..."
curl -s "$BROKER_URL/workers" | python3 -m json.tool
echo ""

# 4. Check credits
echo "4. Checking credits for $USER_ID..."
curl -s "$BROKER_URL/credits/$USER_ID" | python3 -m json.tool
echo ""

# 5. Estimate price
echo "5. Estimating price for 2 CPUs, 4 GiB, 5 seconds..."
curl -s -X POST "$BROKER_URL/price" \
  -H "Content-Type: application/json" \
  -d '{
    "cpus": 2.0,
    "memory_bytes": 4294967296,
    "gpus": 0,
    "estimated_duration_secs": 5.0
  }' | python3 -m json.tool
echo ""

# 6. Add credits
echo "6. Adding 50 credits..."
curl -s -X POST "$BROKER_URL/credits/$USER_ID/add" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 50.0,
    "description": "Demo top-up"
  }' | python3 -m json.tool
echo ""

# 7. Check updated credits
echo "7. Checking updated credits..."
curl -s "$BROKER_URL/credits/$USER_ID" | python3 -m json.tool
echo ""

echo "========================================"
echo "  Demo Complete!"
echo "========================================"
echo ""
echo "To execute a function via the broker, run:"
echo ""
echo "  python3 -c \""
echo "  import zakuro as zk"
echo "  "
echo "  @zk.fn"
echo "  def hello():"
echo "      return 'Hello from remote worker!'"
echo "  "
echo "  compute = zk.Compute(uri='zc://localhost:9001', cpus=1)"
echo "  print(hello.to(compute)())"
echo "  \""
echo ""
