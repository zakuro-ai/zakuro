#!/bin/sh
# Initialize credits for the 2-node P2P mesh
#
# Seeds credits on both node brokers so users can execute
# remote tasks that cost credits.

set -e

echo ""
echo "Initializing mesh credits..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

seed_credits() {
    broker_url="$1"
    api_key="$2"
    user="$3"
    amount="$4"

    response=$(curl -sf \
        -X POST \
        -H "Content-Type: application/json" \
        -H "X-Api-Key: $api_key" \
        -d "{\"amount\": $amount, \"description\": \"Initial mesh credits\"}" \
        "$broker_url/credits/$user/add" 2>/dev/null || echo '{"error": "failed"}')

    if echo "$response" | grep -q '"status":"ok"'; then
        balance=$(echo "$response" | sed -n 's/.*"new_balance":\([0-9.]*\).*/\1/p')
        printf "  [%s] %-15s  +%.2f credits  (balance: %s)\n" "$(echo "$broker_url" | sed 's|.*//||;s|:.*||')" "$user" "$amount" "$balance"
    else
        echo "  $broker_url $user  FAILED: $response"
    fi
}

# Node 1: seed credits
seed_credits "http://node1-broker:9000" "node1-key" "node1-user" "10.0"
seed_credits "http://node1-broker:9000" "node1-key" "test" "10.0"
seed_credits "http://node1-broker:9000" "node1-key" "demo" "10.0"

# Node 2: seed credits
seed_credits "http://node2-broker:9000" "node2-key" "node2-user" "10.0"
seed_credits "http://node2-broker:9000" "node2-key" "test" "10.0"
seed_credits "http://node2-broker:9000" "node2-key" "demo" "10.0"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Mesh credit initialization complete!"
