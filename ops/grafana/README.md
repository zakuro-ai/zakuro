# Zakuro Grafana dashboards

This directory is the source-of-truth for every dashboard the Zakuro project ships. Edit JSON here, not in the Grafana UI — UI edits are lossy across deploys.

Closes [#129](https://github.com/zakuro-ai/zakuro/issues/129).

## Loading a dashboard

```bash
# Provisioning path (recommended) — Grafana loads dashboards from a
# directory at startup. Drop the JSONs into your Grafana's
# /etc/grafana/provisioning/dashboards/ path or equivalent.
cp ops/grafana/*.json /etc/grafana/provisioning/dashboards/

# Or load via the HTTP API:
curl -X POST "$GRAFANA_URL/api/dashboards/db" \
    -H "Authorization: Bearer $GRAFANA_API_KEY" \
    -H "Content-Type: application/json" \
    -d @ops/grafana/zakuro-overview.json
```

The dashboards expect a Prometheus datasource named `${DS_PROMETHEUS}`. Configure that in your Grafana before importing.

## Dashboard catalogue

| File | Title | What it shows |
|---|---|---|
| `zakuro-overview.json` | Zakuro — runtime overview | All four SLO panels (dispatch latency p99, auth-success rate, HMAC failures, per-worker readiness) + supporting metrics (request rate, queue depth, error rate, auth-fail reasons). |
| `zakuro-tenants.json` | Zakuro — per-tenant | Per-tenant request rate, dispatch rate, p50/p95 latency, 5xx error rate, auth refusals by reason. Use to spot noisy-neighbour or single-customer regressions. |
| `zakuro-workers.json` | Zakuro — per-worker | Per-worker queue depth, dispatch rate, p50/p95 latency, plus a fleet-wide latency-distribution heatmap. Use to identify a single host drifting away from the fleet. |
| `zakuro-auth.json` | Zakuro — auth + secrets | Refusals over time by reason and by tenant, HMAC + signature stat panels with thresholds, top-5 refusal-reason and noisy-tenant bargauges, 401-rate cross-check. Use to triage suspected brute-force or token-leak incidents. |

The SLO targets visualised here are defined in [`docs/observability/slos.md`](../../docs/observability/slos.md). Burn-rate alert thresholds match the rules in [`ops/alerts/zakuro-slos.yml`](../alerts/zakuro-slos.yml).

## Template variables

Every panel respects two filters:

- **Worker** — `worker_id` label. `All` aggregates the fleet; pick one worker to drill down.
- **Tenant** — `tenant_id` label. `All` aggregates tenants; pick one to investigate a customer-specific slowdown.

## Annotations

Deploys appear as vertical lines, derived from `changes(zakuro_http_requests_total{path="/info"}[5m]) > 0` — `/info` is hit on every worker startup, so a spike there means a fresh start. Replace with a proper deploy-event metric when one exists.

## How to add a dashboard

1. Build the dashboard in a local Grafana (`docker run --rm -p 3000:3000 grafana/grafana`).
2. Export via **Dashboard → Share → Export → Save to file**.
3. Strip the auto-generated `__inputs` / `__requires` blocks and replace the datasource UID with `${DS_PROMETHEUS}`.
4. Commit the JSON here + add a row to the catalogue.
5. Update [`docs/observability/slos.md`](../../docs/observability/slos.md) if the dashboard introduces a new SLO panel.

Lossy round-trips through Grafana's UI **drop annotations and template variables**. If you change those, edit the JSON directly.
