#!/bin/bash
# Initialize credits for test users
#
# This script runs once after the broker is ready, creating initial
# wallet balances for all test user accounts.

set -e

BROKER_URL="${ZAKURO_BROKER_URL:-http://broker:9000}"
MASTER_KEY="${ZAKURO_MASTER_KEY:-master-key-change-me}"

# Wait for broker to be ready
echo "Waiting for broker at $BROKER_URL..."
until curl -sf "$BROKER_URL/health" > /dev/null 2>&1; do
    echo "  Broker not ready, waiting..."
    sleep 2
done
echo "Broker is ready!"

# List of test users and their initial credits
declare -A USERS=(
    ["demo"]=5.0
    ["user-1"]=5.0
    ["user-2"]=5.0
    ["user-3"]=5.0
    ["user-4"]=5.0
    ["user-5"]=5.0
    ["bench-user"]=100.0
    ["test"]=10.0
)

echo ""
echo "Initializing credits..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

for user in "${!USERS[@]}"; do
    credits="${USERS[$user]}"

    response=$(curl -sf \
        -X POST \
        -H "Content-Type: application/json" \
        -H "X-Api-Key: $MASTER_KEY" \
        -d "{\"amount\": $credits, \"description\": \"Initial wallet credit\"}" \
        "$BROKER_URL/credits/$user/add" 2>/dev/null || echo '{"error": "failed"}')

    if echo "$response" | grep -q '"status":"ok"'; then
        balance=$(echo "$response" | sed -n 's/.*"new_balance":\([0-9.]*\).*/\1/p')
        printf "  %-15s  +%6.2f credits  (balance: %s)\n" "$user" "$credits" "$balance"
    else
        echo "  $user  FAILED: $response"
    fi
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Credit initialization complete!"
echo ""

# Show worker pricing summary
echo "Worker Pricing Summary:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

workers=$(curl -sf "$BROKER_URL/workers" 2>/dev/null || echo '{"workers":[]}')

if command -v python3 &> /dev/null; then
    echo "$workers" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for w in data.get('workers', []):
    print(f\"  {w['name']:20} {w['status']:8} {w['cpus_available']:.0f} CPUs  \${w['price_per_cpu_sec']:.4f}/cpu-sec\")
" 2>/dev/null || echo "  (Could not parse worker list)"
else
    echo "  (python3 not available for parsing)"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Ready for benchmarks!"
echo "  Run: ./zc2 bench http://localhost:9000"
