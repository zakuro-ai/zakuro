# Zakuro — Engineering Plan

**Version:** 0.3-draft • Paired with [`PRD.md`](PRD.md). This plan translates PRD requirements into sequenced engineering work. Each item references the PRD clause it satisfies; completed items point to the merged PR.

## Legend

- ✅ Merged
- 🚧 In flight
- 🔜 Next up
- 💭 Design required

---

## Measured results so far

All numbers here are observed on a real cluster, not projections.

### Mesh-adaptation experiment (2 workers on the same Mac, HTTP transport, 100 dispatches per stage, `scripts/bench_mesh_adaptation.py`)

**Greedy argmin (`softmax_temperature=0`):**

| stage | wall | worker 0 picks | worker 1 picks | observation |
|---|---|---|---|---|
| warmup | — | — | — | p95: `19 ms`, `16 ms`; recommended `backpressure_threshold = 0.029 s` |
| after warmup | 1.63 s | 0 (0 %) | 100 (100 %) | Greedy commits fully to the 3 ms-faster worker. |
| remove worker 0 | 1.69 s | — | 100 (100 %) | Traffic trivially rebalances to the survivor. |
| readmit worker 0 | 1.61 s | 91 (91 %) | 9 (9 %) | Readmitted worker bootstrap-seeded at 17.1 ms; wins most traffic because it beats worker 1's running EMA. |

**Softmax (`softmax_temperature=0.01`):**

| stage | worker 0 picks | worker 1 picks | observation |
|---|---|---|---|
| after warmup | 60 (60 %) | 40 (40 %) | Soft routing keeps both workers utilised. |
| readmit worker 0 | 44 (44 %) | 56 (56 %) | Rebalances within a single batch of 100. |

### Sakura BERT benchmark (x399 4090 trains, Mac MPS evals, distilbert SST-2, 15 epochs)

Repeated from the PR history for context — also measured, also observed:

| configuration | serial | async | Δ |
|---|---|---|---|
| pre-improvements (zakuro 0.2.2) | 9.49 s | 60.91 s | −549 % |
| + perf patches (`sakura#36`) | 9.49 s | 33.51 s | −253 % |
| + `AdaptiveCompute` + `bp=1` | 18.39 s | 20.40 s | −11 % |
| adaptive, all skips (framework floor) | 18.39 s | **15.80 s** | **+12 %** |

The current `warmup()` would have produced `bp ≈ 0.03 × eval-size-ratio` automatically instead of the hand-tuned `1.0 s` that got us the last row; a follow-up measurement against that hardware is queued.

---

## Phase 0 — Ground state (complete)

The framework floor as of 0.2.3.

| Ref | Item | Status |
|---|---|---|
| F1.1–F1.3 | `@fn`, `@cls`, `Compute`, `ZakuroClient` | ✅ |
| F1.5 | `zk.Worker.spawn()` | ✅ `zakuro-ai/zakuro#92` |
| F2.1 | HTTP transport (FastAPI + httpx) | ✅ |
| F2.2 | QUIC transport (aioquic server + client) | ✅ `#92` |
| F2.3 | URI-based transport selection | ✅ |
| F2.4 | Rust worker-QUIC caller in `zc` | ✅ `zakuro-ai/zc#31` |
| F3.1–F3.4 | `AdaptiveCompute` Adam-style allocator | ✅ `#93` |
| F5.1 (partial) | `SakuraHFCallback` lazy drain, fp16, cache, backpressure | ✅ `zakuro-ai/sakura#36` |
| NF3 | Standalone fallback (`ZAKURO_STANDALONE=force`) | ✅ |
| NF4 | `import zakuro` without `[worker]` extras | ✅ |

---

## Phase 1 — Mesh awareness (in progress)

**Goal:** the allocator reacts to grid changes within seconds.

### 1.1 Health-aware dispatch — F3.5 🚧

- Add a heartbeat loop on `AdaptiveCompute` that polls each worker's `HEALTH` opcode every N seconds. Low priority, coalesced into a single stream per worker.
- On failed / slow heartbeat, raise that worker's effective latency by a penalty factor (e.g. `×10`) without permanently marking it dead. A single missed probe should not kill a worker.
- Three strikes → worker ejected from the pool; traffic rebalanced immediately. Readmission after a clean probe.

**Test plan**
- Spin up two workers, kill one mid-run with `SIGSTOP`. Expect dispatch rate to drop to zero on the stopped worker within 15 s.
- `SIGCONT` the worker; expect traffic to resume within 30 s.

### 1.2 Performance drift detection 💭

- When a worker's latency EMA climbs past its historical p95, mark "suspect" and deprioritise (soft demote).
- Use the variance EMA (already tracked) as the threshold signal: `m_hat + 3 × sqrt(v_hat)`.
- Recovery rule: two consecutive dispatches below the historical median → un-demote.

### 1.3 Node admission / departure API — F4.2, F4.3 ✅

- `AdaptiveCompute.add_worker(compute)` / `.remove_worker(idx)` — thread-safe mutation of the pool.
- New workers receive a bootstrap prior from mesh-median latency rather than the static `initial_latency` default. Seeded as `m = median × (1 − β₁)` so that the bias-corrected `m̂` *equals* the median on step 1.
- Hook for a future discovery source (`Tailscale peer list`, `K8s service endpoints`, etc.) to push changes.
- Refuses to drop the last worker — returns `ValueError("cannot remove the last worker; add a replacement first")`.

### 1.4 QUIC connection migration — F4.2 💭

- Verify `aioquic` and `quinn` honour connection migration when the local socket's address changes.
- Write a network-change integration test (`tc netem` or a local TUN reshuffle) that proves an in-flight stream survives.
- Expose a callback on the `QuicProcessor` that fires on `ConnectionIdsIssued` / path-change events for observability.

---

## Phase 2 — Warmup (priority after Phase 1)

**Goal:** every training run starts with calibrated per-worker priors.

### 2.1 `AdaptiveCompute.warmup(rounds=3, timeout=10s)` — F4.1 ✅

Runs `rounds` successful EXECUTE round-trips per worker against a cheap
identity probe. Observed latencies seed each worker's EMA, replacing the
pessimistic `initial_latency` bootstrap; workers that fail every probe
inside `timeout` are ejected from the pool.

- `recommended_backpressure = 1.5 × max(observed_worker_p95)`.
- Workers that respond get their stats re-initialised with `(m=mean_latency, step=rounds)` so post-warmup routing is backed by real observations from the first dispatch.
- `eject_on_failure=True` drops unreachable peers before real traffic lands.

Measured example (two zakuro-worker processes on the same Mac, HTTP transport):

```text
[warmup] zakuro://127.0.0.1:58332 (p95=0.019s),
         zakuro://127.0.0.1:58335 (p95=0.016s)
[warmup] recommended backpressure: 0.03s
```

The 30 ms threshold is ~1.5× the slower worker's p95, i.e. the number
`SakuraHFCallback(backpressure_threshold=...)` used to take as a hand-tuned
argument now gets derived from observation.

The richer liveness/bandwidth probes listed in the original plan are
future-work; the current warmup uses a single probe shape that's sufficient
to seed priors and detect dead nodes.

### 2.2 CLI command `zc bench mesh` 💭

- Run the same warmup flow as a standalone tool for cluster operators.
- Output: JSON report to stdout, suitable for piping into the broker's config store.

---

## Phase 3 — Truly async training (the vision)

**Goal:** training never blocks on anything decouplable.

### 3.1 Checkpoint-handle queue — F5.1 🔜

Replace the "cloudpickle state_dict in callback" pattern with a **non-blocking checkpoint handle** flow:

```
Training step
  → asyncio.Queue.put_nowait(checkpoint_handle)     # non-blocking
  → continue training
Evaluator pool
  → async for handle in queue:
        dispatch handle to AdaptiveCompute
        yield metrics via callback / async iterator
```

Checkpoint handles can be:
- In-memory `torch.Tensor` views (zero-copy on same host).
- Disk paths written by a dedicated CUDA stream (GPU → local SSD, non-blocking).
- Object-store URIs (for multi-machine).

The training loop never owns a cloudpickle operation; the evaluator dispatcher does.

### 3.2 Async metric computation side-pool — F5 💭

A metric is "last hop": after the evaluator has produced `(logits, labels)`, slow metric computations (seqeval, BLEU, BERTScore) go to a **second** pool. Users expose callbacks `on_prediction_ready` (fast) and `on_metric_ready` (slow, arrives later).

Benefits: fast metrics (accuracy, loss) surface immediately; slow ones don't hold up the next dispatch.

### 3.3 Framework integrations for async eval — F6 🔜

- Lightning: drop in an `AsyncCheckpointCallback` that publishes handles; remove the current `ThreadPoolExecutor`-inside-callback pattern.
- HF Trainer: replace `SakuraHFCallback`'s cloudpickle pattern with the handle-queue approach. Only serialise on the worker thread.
- Keras: use `ModelCheckpoint`'s filepath hook to generate handles.

---

## Phase 4 — Grid performance awareness

**Goal:** the allocator's context is rich enough to make good decisions on heterogeneous networks.

### 4.1 Bandwidth map 💭

- Warmup probes produce per-worker bandwidth estimates.
- Dispatch decisions incorporate `expected_transfer_time = payload_size / bandwidth` on top of the latency EMA.
- Critical for training with large state-dicts (distilbert = ~270 MB; bert-large = ~1.3 GB).

### 4.2 Topology hints 💭

- Explicit network-segment tags on `Compute` (`region="us-west"`, `rack="A1"`).
- Allocator prefers same-region when tied on latency.

### 4.3 Price-aware routing — F3.6 💭

- `Compute(price_per_hour=…)` hint.
- `AdaptiveCompute(cost_coefficient=0.3)` — weight $ vs time.
- Useful for mixed on-prem / cloud clusters.

---

## Phase 5 — Protocol evolution

### 5.1 State-dict delta encoding 💭

- Workers cache the previous state-dict they received.
- On subsequent dispatch, the trainer sends only the delta.
- For full fine-tuning, deltas can be ≥ 50 % smaller than full state dict after the first epoch.
- Requires the persistent-validator pattern to already be in place (cache_key).

### 5.2 Streaming multi-chunk payloads 💭

- Current: one frame per call, blocking on full body.
- Proposed: chunked stream; worker can start processing while the remaining bytes arrive.
- Especially important for multi-gigabyte state transfers.

### 5.3 mTLS between mesh peers 💭

- Replace "SkipVerify + out-of-band identity" with real mutual TLS.
- Peer identity keyed on Tailscale / WireGuard public keys, or X.509 certs issued by a small in-cluster CA.

---

## Phase 6 — Observability & tooling

### 6.1 Allocation decision log — NF1 🔜

- Every dispatch writes a line to `~/.zakuro/allocator.jsonl`:
  ```json
  {"t": 1702, "fn": "train_step", "picked": 2, "expected": 1.2, "actual": 1.3, "ok": true}
  ```
- Replay tool: `zc allocator replay <log>` visualises decisions and counterfactuals.

### 6.2 OpenTelemetry integration 💭

- Emit metrics (`zakuro.dispatch.latency`, `zakuro.worker.queue`, `zakuro.backpressure`).
- Traces for each dispatch span with worker tags.

### 6.3 Grafana dashboard 💭

- Worker latency heatmap, queue depth over time, backpressure events, failure rate.

---

## Phase 7 — Framework surface polish

### 7.1 API stabilisation 🔜

- Lock the public API surface in a `zk.public` namespace that's guaranteed stable.
- Deprecation policy: 2 minor releases' warning before removal.

### 7.2 Documentation site 💭

- `docs.zakuro.ai` with a Getting Started guide, recipes, PRD / PLAN, PROTOCOL.md rendered.

### 7.3 Example gallery 💭

- MNIST (done — `notebooks/standalone_mode.ipynb`, `notebooks/two_worker_demo.ipynb`).
- BERT benchmark (done — `bert_demo/bench_bert.py`).
- 🔜 PyTorch DDP — 8-GPU async eval example.
- 🔜 Diffusion fine-tuning with async sample-grid eval.

---

## Cross-cutting concerns

### Testing matrix 🔜

| Layer | Coverage target | Today |
|---|---|---|
| `AdaptiveCompute` | 90 % | 91 % (test_adaptive.py) |
| QUIC processor | 80 % | 90 % |
| HF callback | 80 % | ~60 % |
| Mesh warmup (Phase 2) | 80 % | not yet built |
| End-to-end cross-machine | smoke test in CI | manual |

### Release cadence 🔜

- Minor every 6 weeks (0.3, 0.4, …).
- Patch releases for critical bugs on any supported minor.
- Wire protocol version bumps only at major boundaries.

### Dependency hygiene

- Core (`pip install zakuro-ai`): no ML-library imports.
- `[worker]` extra pulls FastAPI, uvicorn, psutil, aioquic.
- `[ml]` meta-extra installs the Sakura integrations.

---

## Sequencing & priorities

```
┌────────────── Phase 0: DONE ─────────────┐
                    │
                    ▼
          Phase 1: Mesh awareness
          (health, drift, churn)
                    │
                    ▼
            Phase 2: Warmup
            (calibrated priors)
                    │
                    ▼
        Phase 3: Truly async training
        (checkpoint-handle flow)
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
    Phase 4      Phase 5     Phase 6
    (grid)    (protocol)    (obs)
                    │
                    ▼
              Phase 7 (polish)
```

Phase 1 is on the critical path because every subsequent phase assumes a working "detect node change, react within seconds" primitive. Phase 2 sharpens the cold-start story. Phase 3 is the biggest user-visible win (true async training). Phases 4–6 unlock larger meshes. Phase 7 is ongoing.

## Exit criteria for 1.0

- All PRD success metrics (SM1–SM5) met on the reference benchmark mesh (2 GPUs + 2 mixed CPU workers + 1 flaky worker simulated with `tc netem`).
- Wire protocol frozen at v1; subsequent changes require a new ALPN.
- Documentation site live with Getting Started ≤ 5 minutes from zero.
- ≥ 3 external users running non-trivial workloads.
