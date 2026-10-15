#!/bin/bash
# Seed yen balances for test users
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

# Test users and their initial balance (yen)
declare -A USERS=(
    ["demo"]=750.0
    ["user-1"]=750.0
    ["user-2"]=750.0
    ["user-3"]=750.0
    ["user-4"]=750.0
    ["user-5"]=750.0
    ["bench-user"]=15000.0
    ["test"]=1500.0
)

echo ""
echo "Initializing balances..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

for user in "${!USERS[@]}"; do
    amount="${USERS[$user]}"

    response=$(curl -sf \
        -X POST \
        -H "Content-Type: application/json" \
        -H "X-Api-Key: $MASTER_KEY" \
        -d "{\"amount\": $amount, \"description\": \"Initial balance\"}" \
        "$BROKER_URL/balance/$user/add" 2>/dev/null || echo '{"error": "failed"}')

    if echo "$response" | grep -q '"status":"ok"'; then
        balance=$(echo "$response" | sed -n 's/.*"new_balance":\([0-9.]*\).*/\1/p')
        printf "  %-15s  +¥%8.2f  (balance: %s)\n" "$user" "$amount" "$balance"
    else
        echo "  $user  FAILED: $response"
    fi
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Balance initialization complete!"
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
    print(f\"  {w['name']:20} {w['status']:8} {w['cpus_available']:.0f} CPUs  ¥{w['price_per_cpu_sec']:.4f}/cpu-sec\")
" 2>/dev/null || echo "  (Could not parse worker list)"
else
    echo "  (python3 not available for parsing)"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Ready for benchmarks!"
echo "  Run: ./zc2 bench http://localhost:9000"
