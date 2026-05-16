# RFC 0003 — Observability stack: hybrid Prom + OTel + structlog

- **Status:** Accepted (2026-05)
- **Closes:** [#123](https://github.com/zakuro-ai/zakuro/issues/123), [#124](https://github.com/zakuro-ai/zakuro/issues/124), [#125](https://github.com/zakuro-ai/zakuro/issues/125)
- **Related:** [#127](https://github.com/zakuro-ai/zakuro/issues/127) (SLOs), [#129](https://github.com/zakuro-ai/zakuro/issues/129) (Grafana dashboards)
- **Depends on:** [RFC 0002](0002-auth-mtls-jwt.md) (request-id / tenant-id flow through every log + span)

## Context

`#128` (Sentry) is live for unhandled errors. Everything else is missing:

- No structured logs — `print()` and stdlib `logging` produce free-form text that's unsearchable across N workers.
- No metrics — drift detection, queue depth, dispatch latency are computed in-memory and lost on restart.
- No tracing — when a request takes 5 s, there's no way to see which worker / which step / which queue.

The user picked the **hybrid** approach over OTel-only or Sentry-extended in the May 2026 question round. Rationale: each tool stays on its terrain of strength, and the runtime cost is the lowest of the four options.

## Decision

| Signal | Stack | Library | Sink |
|---|---|---|---|
| **Logs** | structlog JSON | [`structlog`](https://www.structlog.org/) | stdout → fluentbit/vector → backend of choice |
| **Metrics** | Prometheus | [`prometheus_client`](https://github.com/prometheus/client_python) | scrape `/metrics` on the worker + broker |
| **Traces** | OpenTelemetry | `opentelemetry-sdk` + `opentelemetry-exporter-otlp` | OTLP/HTTP to a collector |
| **Errors** | Sentry (existing) | `sentry-sdk` (already wired) | sentry.zakuro.ai |

A single shared `request_context` (request-id, tenant-id, worker-id, trace-id, span-id) propagates across all four sinks so a log line, a metric label, a span, and a Sentry event for the same request can be correlated by joining on `request-id`.

## Implementation plan

The work splits into four parallel tracks. Each can land independently.

### Track A — structlog (#125)

**New module** `zakuro/observability/logging.py`:

```python
def init_logging(component: str, level: str = "INFO") -> None:
    """Configure structlog + stdlib logging to emit JSON to stdout."""
```

- Renders to JSON with keys: `time` (ISO 8601), `level`, `component` (`worker` / `client` / `broker`), `event`, plus every field from `_REQUEST_CONTEXT` from `zakuro.observability.sentry`.
- Replaces the stdlib `logging` handlers so `logger = structlog.get_logger(__name__)` and `logging.getLogger(__name__)` both produce JSON.
- PII scrubber: reuse `zakuro.observability.sentry._redact_pii_in_string` via a structlog processor so emails / IPs / paths are redacted **before** they leave the process.
- Hooked into worker startup right after `init_sentry("worker")`, into client at first `ZakuroClient` construction.

JSON schema (one line per record):

```json
{
  "time":      "2026-05-16T14:23:08.412Z",
  "level":     "info",
  "component": "worker",
  "event":     "job_completed",
  "request-id":"req-0c12...",
  "tenant-id": "tenant-acme",
  "worker-id": "worker-3",
  "trace-id":  "5a8b...",
  "span-id":   "2f1c...",
  "duration_ms": 142
}
```

Keys are stable across versions (see [`docs/STABILITY.md`](../STABILITY.md) → "Log schema" section to add).

### Track B — Prometheus (#124)

**New module** `zakuro/observability/metrics.py`:

```python
HTTP_REQUESTS_TOTAL = Counter(
    "zakuro_http_requests_total",
    "HTTP requests handled by the worker",
    labelnames=("method", "path", "status", "tenant_id"),
)

DISPATCH_LATENCY_SECONDS = Histogram(
    "zakuro_dispatch_latency_seconds",
    "End-to-end function dispatch latency",
    labelnames=("worker_id", "tenant_id"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

WORKER_QUEUE_DEPTH = Gauge(
    "zakuro_worker_queue_depth",
    "Pending jobs in the worker's thread pool",
    labelnames=("worker_id",),
)
```

Naming convention follows the [Prometheus best practices](https://prometheus.io/docs/practices/naming/): `<namespace>_<subsystem>_<name>_<unit>`.

**Label cardinality budget:** tenant-id is bounded (~hundreds), worker-id is bounded (~tens), path is enumerated. We refuse to add labels with unbounded cardinality (raw user IDs, request IDs, IPs).

`/metrics` endpoint added to the worker FastAPI app and the broker. The endpoint is protected by mTLS + the `metrics:read` JWT scope (RFC 0002).

### Track C — OpenTelemetry tracing (#123)

**New module** `zakuro/observability/tracing.py`:

```python
def init_tracing(component: str) -> None:
    """Initialise an OTLP HTTP tracer provider when OTEL_EXPORTER_OTLP_ENDPOINT is set."""
```

- Read `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME` from env (industry-standard env vars).
- Tail-sampling at 10% to start; tune after the first week of production data.
- Auto-instrumentation via `opentelemetry-instrumentation-fastapi` and `opentelemetry-instrumentation-httpx` covers the HTTP transport surface for free. QUIC needs hand-rolled spans in `zakuro.worker.quic_server.handle_connection`.

**Span naming convention:**

| Span name | Where | Attributes |
|---|---|---|
| `zakuro.client.execute` | client side of dispatch | `tenant_id`, `worker_id`, `fn_qualname` |
| `zakuro.worker.execute` | worker `/execute` handler | `tenant_id`, `worker_id`, `job_id` |
| `zakuro.adaptive.pick_worker` | client allocator | `worker_id_chosen`, `worker_count` |
| `zakuro.wire.pack` / `unpack` | wire-format codec | `payload_bytes`, `format` |

Trace-id is injected into the W3C `traceparent` header on every outbound HTTP call so the trace stitches across the broker without code changes.

### Track D — request context propagation (cross-cutting)

`zakuro.observability.context.set_request_context(...)` already exists from [#128](https://github.com/zakuro-ai/zakuro/issues/128) (Sentry tags). Extend it:

- Wire the structlog processor so every log line picks up `request-id` / `tenant-id` / `worker-id` from the same `ContextVar`.
- Wire the Prometheus label extractor so counters/histograms carry tenant-id automatically.
- The OTel tracer reads the same ContextVar and sets baggage so child spans inherit it.

One source of truth → no drift between log tags, metric labels, span attributes.

## Rejected alternatives

| Option | Why rejected |
|---|---|
| OTel-only (logs + metrics + traces over OTLP) | Single pipeline is elegant but the OTel logs SDK is the least mature of the three; structlog is much more battle-tested for JSON log emission. Re-evaluate in 12 months. |
| Sentry for tracing too | Sentry's trace coverage is excellent for errors-with-context but its metric story is thin (no histograms with native buckets). And we'd pay Sentry SaaS rates for every span. |
| Defer to first customer | Was an option on the questionnaire. Rejected — the SLOs in [#127](https://github.com/zakuro-ai/zakuro/issues/127) require *some* metric backend to exist before they can be defined, and the [`PLAN.md`](../../PLAN.md) headline numbers are currently in-memory + ephemeral. |
| ELK stack | Heavyweight on the indexing tier. We'd need to maintain Elasticsearch. Out of scope while a single team is shipping. |

## Migration / rollout

Each track ships as its own PR — they don't share files. Order:

1. **structlog (Track A)** — lowest risk, replaces `logging` calls only. Lands first because every subsequent track wants to log structured.
2. **Prometheus (Track B)** — adds a `/metrics` endpoint and a few counter increments. Needs the `metrics:read` JWT scope from RFC 0002.
3. **OTel (Track C)** — adds spans; opt-in via env var so a missing OTel collector doesn't break the worker.
4. **Grafana dashboards (#129)** + **SLO definitions (#127)** — land after Tracks A–C are in production for at least a week of real data.

The pre-existing Sentry wiring stays untouched. `zakuro.observability` becomes the single import surface (`init_sentry`, `init_logging`, `init_tracing`, `metrics` namespace).

## Open questions for implementation time

- **OTel backend choice** (Tempo? Honeycomb? Datadog? self-hosted Jaeger?) — defer to whoever owns the cluster operations. The exporter is OTLP/HTTP regardless.
- **Log retention.** structlog emits JSON to stdout; retention is a deployment concern. Pinpoint when there's a real budget.
- **Metric scrape model** (Prom-pull vs. OTel-push). Start with Prom-pull (simpler, matches the cluster's existing Prom). Switch to OTLP-push later only if cross-cluster federation forces it.
- **Cardinality alarms.** Add a Prometheus alert on `prometheus_tsdb_head_series` so we catch label cardinality blowups before they OOM the TSDB.
