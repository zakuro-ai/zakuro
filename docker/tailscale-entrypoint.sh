#!/bin/sh
# Fetch Tailscale auth key from the Zakuro dashboard API using ZAKURO_API_KEY.
# Falls back to TS_AUTHKEY if already set (e.g. during local testing).

if [ -z "$TS_AUTHKEY" ]; then
    if [ -z "$ZAKURO_API_KEY" ]; then
        echo "[tailscale-entrypoint] ERROR: ZAKURO_API_KEY not set and TS_AUTHKEY not provided"
        exit 1
    fi

    API_URL="${ZAKURO_API_URL:-https://my.zakuro-ai.com}"
    echo "[tailscale-entrypoint] Fetching Tailscale auth key from ${API_URL}..."

    TS_AUTHKEY=$(wget -qO- "${API_URL}/api/broker/config/tailscale-key" \
        --header "X-Broker-Api-Key: ${ZAKURO_API_KEY}" \
        | sed 's/.*"tailscale_auth_key":"\([^"]*\)".*/\1/')

    if [ -z "$TS_AUTHKEY" ] || [ "$TS_AUTHKEY" = "null" ]; then
        echo "[tailscale-entrypoint] ERROR: Could not fetch Tailscale auth key from API"
        exit 1
    fi

    export TS_AUTHKEY
    echo "[tailscale-entrypoint] Tailscale auth key fetched successfully"
fi

exec /usr/local/bin/containerboot "$@"
