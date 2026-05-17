# SLOs and burn-rate alerts

**Status:** Initial (2026-05). Maintainer: oncall@zakuro.ai. Refresh cadence: every release with measured-numbers data; threshold review quarterly.

Closes [#127](https://github.com/zakuro-ai/zakuro/issues/127). Wires the Prometheus metrics from [PR #185 / #124](https://github.com/zakuro-ai/zakuro/pull/185) into a Service-Level Objective set and the burn-rate alerts that page oncall when an SLO is being eaten faster than the error budget allows.

The Grafana dashboards that visualize these live alongside in `ops/grafana/zakuro-overview.json` (#129).

## How to read this document

- Every SLO row carries: an **Objective** (what we promise), an **SLI** (how we measure it), a **target** (the percentile we commit to), a **window** (over how long), and an **error budget** (how much we can fail in that window).
- Burn-rate alerts use the **multi-window, multi-burn-rate** pattern from Google's SRE Workbook. Two windows per SLO (a fast 1-hour window for outages, a slow 6-hour window for slow drift) — both must fire to page. This trades a few minutes of detection latency for ~zero false positives during routine traffic blips.

## SLO catalogue

### SLO-1 — Dispatch latency

**Objective:** 99% of successful dispatches complete within 250 ms server-side (worker `/execute` handler wall clock).

| Field | Value |
|---|---|
| **SLI source** | `zakuro_dispatch_latency_seconds_bucket` histogram (#124) |
| **Target** | 99.0% |
| **Window** | 30 days rolling |
| **Error budget** | 1.0% of dispatches |
| **Indicator query** | `sum(rate(zakuro_dispatch_latency_seconds_bucket{le="0.25"}[5m])) / sum(rate(zakuro_dispatch_latency_seconds_count[5m]))` |

**Why 250 ms / 99%?** Measured numbers from the runtime tracking board: laptop-only QUIC dispatch is ~150 ms p95 cold; cross-container dispatch is ~130 ms. Setting the SLO at 250 ms / 99% leaves enough headroom for two layers of network and a small queue at the broker while making cold-start regressions visible. Tighten to 150 ms once the dispatch path has 4 weeks of steady-state data at v0.4.

### SLO-2 — Auth success rate

**Objective:** ≥ 99.5% of requests that present a valid bearer token reach the handler (no false-401s).

| Field | Value |
|---|---|
| **SLI source** | `zakuro_http_requests_total` (#124) + `zakuro_auth_failure_total` (#124 / #116) |
| **Target** | 99.5% |
| **Window** | 30 days rolling |
| **Error budget** | 0.5% of authed requests |
| **Indicator query** | `1 - (sum(rate(zakuro_auth_failure_total[5m])) / sum(rate(zakuro_http_requests_total[5m])))` |

**Why 99.5%?** The auth code path is small and well-tested; expected false-positive rate is dominated by clock skew on JWT verification (`leeway_seconds=5` allows ~25 s window). 99.5% gives ~3.6 hours of degraded-auth budget per month, enough to absorb a single rotation that goes sideways without paging.

### SLO-3 — Worker availability

**Objective:** ≥ 99.9% of `GET /ready` probes return 200 within 2 seconds, per worker.

| Field | Value |
|---|---|
| **SLI source** | `zakuro_http_requests_total{path="/ready"}` (#124) |
| **Target** | 99.9% |
| **Window** | 30 days rolling |
| **Error budget** | 0.1% per worker per month — ~43 minutes |
| **Indicator query** | `sum(rate(zakuro_http_requests_total{path="/ready",status=~"2.."}[5m])) / sum(rate(zakuro_http_requests_total{path="/ready"}[5m]))` |

**Why per worker?** The mesh tolerates individual worker failures (RFC 0008 gossip auto-routes), so the *aggregate* availability is much higher than 99.9% as long as individual workers hold this. Per-worker SLO surfaces the slow-burning failure (an individual worker degrading) before the mesh has to compensate.

### SLO-4 — Wire-format integrity

**Objective:** Zero envelopes per million pass postcard decode but fail HMAC.

| Field | Value |
|---|---|
| **SLI source** | `zakuro_auth_failure_total{reason="hmac"}` |
| **Target** | < 0.0001% (effectively zero) |
| **Window** | 7 days rolling |
| **Error budget** | 0 — any non-zero HMAC failure rate is an incident |
| **Indicator query** | `sum(increase(zakuro_auth_failure_total{reason="hmac"}[5m]))` |

**Why a hard zero?** A non-zero rate means either (a) a misconfigured key on the broker side, (b) clock-related JWT issuance bugs leaking into the HMAC derivation, or (c) someone is trying to forge envelopes. None of those are "burn the budget for a while and decide later" — they page immediately.

## Burn-rate alerts

Following the SRE Workbook's multi-window multi-burn-rate pattern. For each non-zero-budget SLO we ship two alerts:

| Alert | Trigger | Page severity |
|---|---|---|
| `<SLO>FastBurn` | Burn ≥ **14.4×** the steady-state error budget over the last **1 hour** AND over the last **5 minutes** | P1 page |
| `<SLO>SlowBurn` | Burn ≥ **6×** the steady-state error budget over the last **6 hours** AND over the last **30 minutes** | P2 ticket |

Burn rate of 1× = consuming the monthly budget exactly across the month. 14.4× consumes it in 2 hours; 6× consumes it in 5 days. The "AND" condition across two windows is the false-positive suppressor.

SLO-4 (wire-format integrity) gets a single hard-fire rule: any non-zero count over 5 minutes pages immediately.

The Prometheus rule file lives at [`ops/alerts/zakuro-slos.yml`](https://github.com/zakuro-ai/zakuro/blob/master/ops/alerts/zakuro-slos.yml).

## Error-budget policy

When an SLO has consumed > 50% of its budget for the current window:

1. **Freeze risky merges.** The CI lane that publishes a release runs `python ops/check_slo_freeze.py` (forthcoming — issue tracked in [#127](https://github.com/zakuro-ai/zakuro/issues/127)) and fails if any SLO is over 50%. Override requires a maintainer note in the PR.
2. **Page-out blast radius decisions go to oncall.** No "we'll just ship it; the budget will recover" without an explicit oncall sign-off.

When an SLO has *exhausted* its budget (≥ 100% consumed) before window rollover:

1. **All deploys halted** except security/SLO-fix patches.
2. **Post-mortem within 5 business days** with the SLO-recovery plan (or a documented decision to lower the SLO).

## Process

- Every release reviews the past-window SLO consumption and either keeps the target, raises it (we got lucky), or lowers it (it was unrealistic). The decision lands in the release-notes header.
- New SLOs are proposed via RFC, not as PRs to this file directly — the discussion of "is this the right thing to commit to" deserves its own RFC.

## References

- [Google SRE Workbook — Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)
- [Prometheus best practices — recording rules](https://prometheus.io/docs/practices/rules/)
- [`docs/ci.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/ci.md) — CI lane required-vs-advisory; deploy freeze interacts with this
- [`docs/security/threat-model.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/security/threat-model.md) §6 — SLO-4 ties to the wire-format gate
