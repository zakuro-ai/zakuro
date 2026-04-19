# Zakuro — Product Requirements Document

**Version:** 0.3-draft • **Status:** working document • **Owner:** Zakuro core team

## 1. Product pitch

Zakuro is the **context-aware distributed ML runtime** for modern workloads: heterogeneous GPU meshes, flaky networks, intermittent cloud workers, multi-tenant clusters. It replaces the "pin-each-job-to-a-worker-and-hope" control plane of existing frameworks with an **online allocator** that learns each node's performance, routes in-flight, and never makes training wait on anything that can be decoupled.

It is built on three non-negotiable principles:

1. **Async-first execution.** Training never blocks on evaluation, checkpointing, logging, or metric computation. Anything that *can* be handed to a side pool *is* handed to a side pool, and the main loop keeps stepping.
2. **Adaptive, context-aware allocation.** Every dispatch is a decision informed by running estimates of each worker's latency, variance, queue depth, failure rate, and network cost. The allocator reacts to grid changes (nodes dropping, GPUs getting hotter, network jitter) within seconds.
3. **Protocol-level optimisation.** QUIC is the default transport — multiplexed streams, 0-RTT resumption, connection migration on network change. HTTP is supported for interoperability but never the primary path.

## 2. Who it's for

| Persona | Pain today | What Zakuro gives them |
|---|---|---|
| ML researcher on a shared cluster | Waits 30 min for a node; peer jobs degrade latency silently. | Auto-reroutes around slow nodes; workload never blocks on a bad node. |
| Small team running multi-region training | Spot preemptions abort training; eval backlog crashes the run. | Preemption = replace worker, retry fan-out; eval is async and backpressured. |
| Platform engineer supporting ML teams | Has to hand-tune routing strategies per team. | Allocator is self-tuning; no strategy flags to pick. |
| Academic lab with mixed GPU generations | Scheduler treats a K80 and an H100 as interchangeable. | Allocator learns per-worker throughput and routes accordingly. |

Non-users: single-GPU single-host workflows (use PyTorch directly — Zakuro is overkill).

## 3. Principles in detail

### 3.1 Async-first is the law, not a feature

There is no valid reason for a training loop to wait on eval, on logging, on checkpoint upload, on metric computation. Every one of those is a **notification**, not a barrier.

Concretely:

- **Training ↔ Evaluation.** Training writes checkpoints (to disk or directly as a state-dict handle). An evaluator pool watches for new checkpoints, processes them, publishes metrics back through a non-blocking queue. Training *never* stalls for a missing metric.
- **Training ↔ Logging.** Metrics go into a bounded async queue. Overflow drops, never blocks.
- **Training ↔ Checkpointing.** Checkpoint is async-copied off the GPU using a dedicated stream; main training step never waits on disk.
- **Eval ↔ Metric computation.** Slow metrics (BERTScore, seqeval, BLEU-4) run in their own side-pool. Eval returns *logits + labels* first; metrics land later with the same checkpoint handle.

This is not optional behaviour the user opts into — it is the framework's default. The only "sync" operation the user can explicitly ask for is a `flush()` at the end of a run.

### 3.2 Adaptive allocation is the only allocation

Zakuro does **not** ship routing strategies. No `best_price`, no `round_robin`, no `best_latency`. The allocator observes, learns, and routes.

- **Online EMA of latency and variance per worker.** Adam-style (β₁, β₂, bias correction). The current `AdaptiveCompute` already does this.
- **Queue-depth-aware dispatch.** Expected time-to-serve = `(queue + 1) × EMA_latency`. Always pick argmin.
- **Soft allocation under uncertainty.** When two workers' expected times overlap, sample softmax-weighted. Prevents pinning early.
- **In-context decisions.** The allocator's state *is* the context — a vector of per-worker observations. Unlike ML models that need retraining, this "context" is always fresh because it's just running statistics.

### 3.3 The mesh is not static

A Zakuro cluster is *not* "N workers that never change." Nodes drop, return, get slow, get replaced. The allocator treats this as ambient noise:

- **Health monitoring** runs continuously; a failed health check raises the worker's effective latency, naturally deflecting traffic.
- **Performance drift detection** — a worker whose variance EMA diverges from its mean gets marked "suspect" and deprioritised.
- **Connection migration** — when a QUIC connection breaks (network change, NAT rebinding), QUIC's connection migration completes the round-trip. The allocator sees no failure.
- **Graceful re-integration** — a returning worker rejoins with a pessimistic latency prior (bootstrap from the mesh median), earning back traffic as it demonstrates capability.

### 3.4 Mesh warmup before real work

Before the first real training step, Zakuro runs a **mesh warmup**: a short synthetic workload — a few cloudpickled no-ops and a few small-payload roundtrips — to calibrate per-worker latency, bandwidth, and failure baseline.

Outputs:

- Initial latency EMAs (instead of the pessimistic default prior).
- Bandwidth map (which workers are on which network segment).
- Failure map (nodes that timed out on a trivial task are dropped from the pool before real training starts).
- A *recommended* backpressure threshold, set from observed p95 per worker.

This replaces the "guess a number" approach with data. The user sees a one-line report before training starts:

```
[warmup] 4 workers, 2 fast (mac, x399-a: ~0.3s), 2 slow (mac-b, x399-b: ~5s), bp=8s
```

### 3.5 Transport choices

| Transport | Use |
|---|---|
| **QUIC** (default) | All high-frequency dispatch. Multiplexed streams, TLS 1.3, 0-RTT resumption, connection migration. |
| HTTP/1.1 | Interop path for workers behind reverse proxies that don't speak QUIC. Same wire body (cloudpickle), same semantics. |
| Unix domain sockets | Same-host broker↔worker when deployed in a single box. Zero-copy potential. |

Transport is **observable but not configurable** for the user: `Compute(uri="quic://…")` vs `"zakuro://…"` is a URI choice, not a policy decision. The framework uses whatever the URI declares and measures the result.

### 3.6 Cross-language and cross-platform

- **Rust side** (`zc`) handles HTTP termination, QUIC connection pools, TLS, wire framing, routing. Low-level, performance-critical.
- **Python side** (`zakuro`) handles user API, `@fn` / `@cls` decorators, cloudpickle, user-space allocators. High-level, ergonomic.
- **Protocol is the contract** — both sides read `docs/PROTOCOL.md`. New language bindings (Go, TypeScript for client-only flows) slot in without touching either current side.

## 4. Functional requirements

Numbered for traceability from PLAN.md.

### F1 — User-facing API

- **F1.1** `@zk.fn` decorator, `.to(compute)` attachment, call invocation.
- **F1.2** `@zk.cls` decorator, `RemoteProxy` method forwarding.
- **F1.3** `zk.Compute` single-worker target.
- **F1.4** `zk.AdaptiveCompute` multi-worker context-aware allocator. ✅ *shipped in 0.2.3*
- **F1.5** `zk.Worker.spawn()` convenience wrapper around the CLI.

### F2 — Transport

- **F2.1** HTTP transport (FastAPI server, httpx client). ✅
- **F2.2** QUIC transport (aioquic server, aioquic client; `quic://` URI scheme; ALPN `zk-worker`). ✅
- **F2.3** Transport selection via URI; zero user code change.
- **F2.4** Rust `zc` broker speaks both transports byte-compatibly.

### F3 — Allocation

- **F3.1** Adam-style EMA latency + variance tracking per worker. ✅
- **F3.2** Queue-depth-aware argmin dispatch. ✅
- **F3.3** Softmax-weighted exploration. ✅
- **F3.4** Backpressure signal (`is_backpressured`). ✅
- **F3.5** Health-aware deprioritisation — worker with >30 s since last heartbeat loses a dispatch vote.
- **F3.6** Cost model — when `Compute` carries a `price_per_hour` hint, allocator trades off speed vs cost per task.

### F4 — Mesh lifecycle

- **F4.1** Warmup phase runs a synthetic probe before first real dispatch. Produces per-worker priors for the allocator.
- **F4.2** Node departure — a failed dispatch within N retries triggers worker ejection; remaining workers absorb load.
- **F4.3** Node arrival — new workers admitted via discovery, receive a bootstrap prior, earn traffic.
- **F4.4** Recurring health probes — low-priority QUIC HEALTH calls, no cost when mesh is healthy.

### F5 — Async evaluation

- **F5.1** Training publishes checkpoint handles to an async queue; never waits on eval.
- **F5.2** Evaluator pool watches the queue, routes through `AdaptiveCompute`, surfaces metrics via callback or async iterator.
- **F5.3** Bounded memory — the callback never retains more than `max_pending` checkpoints; overflow policy is user-configurable (`skip` / `drop_oldest` / `block`).

### F6 — Framework integrations

- **F6.1** PyTorch Lightning — `SakuraTrainer`, callback-level integration. ✅
- **F6.2** HuggingFace Trainer — `SakuraHFCallback`, `TrainerCallback` subclass. ✅
- **F6.3** TensorFlow / Keras — `SakuraKerasCallback`, Keras callback. ✅
- **F6.4** PyTorch DDP — evaluator hooks into DDP's hook mechanism (planned).
- **F6.5** JAX — `pmap`-friendly dispatcher (future).

## 5. Non-functional requirements

- **NF1 — Observability.** Every dispatch logs `{worker_id, expected_time, actual_time, success}` to a local file and optionally to OpenTelemetry. Users can replay allocation decisions.
- **NF2 — Deterministic mode.** A `seed` on `AdaptiveCompute` reproduces softmax samples for reproducibility.
- **NF3 — Graceful degradation.** `ZAKURO_STANDALONE=force` makes the whole system run in-process. Useful for CI, unit tests, laptops without network. ✅
- **NF4 — Zero-install cost.** `import zakuro` must succeed without `[worker]` extras. ✅
- **NF5 — Small dependency surface.** Core: `cloudpickle`, `httpx`, `pydantic`. Worker extras: `fastapi`, `uvicorn`, `aioquic`, `psutil`. No transitive ML dependencies.
- **NF6 — Compatibility guarantee.** Wire protocol (PROTOCOL.md) is versioned via ALPN. Breaking changes require a new ALPN; old workers keep running.
- **NF7 — Test coverage.** Core allocator + transport modules ≥ 80 % line coverage; integration tests cover the full train-eval round-trip.

## 6. Out of scope

- Hyperparameter search (orthogonal concern; integrate with Optuna / Ray Tune).
- Model parallelism / FSDP / tensor parallelism — Zakuro handles *jobs*, not intra-step parallelism. (That said, a job can itself be a DDP all-reduce step.)
- Data movement / dataset sharding (out of scope for now; the user is expected to ensure the eval worker can see the data).

## 7. Success metrics

A Zakuro deployment is working if, over a 24-hour training run:

- **SM1** ≤ 1 % of training iterations blocked on eval, checkpointing, or metric computation.
- **SM2** Allocator routes away from a sick worker within 10 s of its latency EMA crossing the p95 threshold.
- **SM3** Mesh warmup identifies 100 % of unreachable workers and produces a usable backpressure threshold without manual tuning.
- **SM4** QUIC connection migration completes transparently across a network-change event (tested via `tc netem`-induced RTT jump).
- **SM5** Single-tenant bench: ≥ 20 % end-to-end wall-clock improvement over the best static-routing baseline on an inhomogeneous 4-node mesh.

## 8. Open questions

- **Global vs per-job allocator.** Should one `AdaptiveCompute` share statistics across every Fn in a process, or is per-Fn isolation cleaner? (Tentative: per-`AdaptiveCompute` instance; opt-in global via `zk.global_allocator()`.)
- **State-dict deltas.** Full state_dict transfer dominates for large models. Worth building a delta encoder?
- **Mesh-level identity.** Workers currently know themselves by name; do we need cryptographic identity for multi-tenant trust?
- **Scheduling policies above the allocator.** Sketch a `Budget` object — user says "up to $10 of compute"; allocator respects.
