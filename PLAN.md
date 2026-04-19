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

### Health-probe experiment (2 Mac workers, HTTP, `scripts/bench_health_detection.py`)

After warmup the bench sends `tiny.to(adaptive)()` at 10 disp/s, then SIGKILLs worker 0. Measured detection latency — time from kill to `stats["suspended"] = True`:

| `probe_interval` | `max_strikes` | measured detection | post-suspend picks worker 0 | worker 1 |
|---|---|---|---|---|
| 0.5 s | 2 | **743 ms** | 7 | 97 |
| 0.1 s | 1 | **18 ms** | 28 | 132 |

The probe loop keeps the pool live-updating without draining the foreground dispatch thread — the suspended worker gets no further traffic despite remaining in `adaptive.workers`.

### Drift-injection experiment (`scripts/bench_drift_detection.py`)

Two Mac workers under softmax routing (τ = 0.05). Baseline at t=0, then 10 s of `sleep(0.25)` injected into every request worker 0 serves. Drift detector engages, traffic deflects, and when the injection stops, softmax plus probe-fed EMA bring worker 0 back:

| stage | 0 picks | 1 picks | drift detected |
|---|---|---|---|
| baseline 3 s | 26 | 19 | — |
| injection 10 s | 8 (5 %) | 142 (95 %) | **t + 0.48 s** |
| recovery 20 s | 167 (56 %) | 133 (44 %) | cleared |

### State-dict serialiser: cloudpickle vs torch.save (x399 CPU, distilbert 268 MB)

Producer-side dump cost — matters because it runs in the pool thread while training is still happening on the main thread:

| serialiser | wall time | concurrent-thread CPU share |
|---|---|---|
| `cloudpickle.dumps(state_dict)` | 481.6 ms | **39 %** of baseline |
| `torch.save(state_dict, BytesIO)` | **282.0 ms** | **72 %** of baseline |

Switching to `torch.save` halves both the wall time and the GIL hold, so the training step gets ~2× as much CPU while the epoch-end snapshot is being packaged.

### Async CUDA-stream snapshot (x399 4090, distilbert fp32, 5-epoch fine-tune)

Main-thread cost per epoch of `on_epoch_end`, measured via
`/tmp/profile_callback_v2.py` with controlled before/after runs:

| variant | main-thread avg | savings |
|---|---|---|
| blocking `.cpu()` (previous) | 176.1 ms | — |
| async CUDA-stream copy + event | **74.8 ms** | **−57.5 %** |

State-dict size: 268 MB fp32. The PCIe transfer still happens; it just no longer stalls the training thread — pool worker syncs on the CUDA event before cloudpickling.

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

### 1.1 Health-aware dispatch — F3.5 ✅

- Background thread started via `AdaptiveCompute.start_health_probes(interval, timeout, max_strikes)`. QUIC workers get the HEALTH opcode; HTTP workers get a `GET /health`.
- Per-worker `health_strikes` counter. On `max_strikes` consecutive misses the worker is **suspended** — `_expected_times_locked` returns `inf` for it, so the picker routes around without ejecting. A single successful probe resets the counter and flips `suspended` back to `False`.
- `probe_once()` exposes one synchronous round for users who prefer to drive their own cadence (tests, integration hooks).
- `__del__` and `stop_health_probes()` reap the thread cleanly.

**Measured: SIGKILL → suspended latency** (`scripts/bench_health_detection.py`, 2 Mac workers, HTTP):

| `probe_interval` | `max_strikes` | observed detection latency |
|---|---|---|
| 0.5 s | 2 | **743 ms** |
| 0.1 s | 1 | **18 ms** |

Faster tuning trades off against probe traffic volume, but even the "loose" setting beats the PRD's SM2 target of ≤ 10 s by more than an order of magnitude.

### 1.2 Performance drift detection — F3 ✅

- Two EMAs per worker: the fast one (`β₁`, current behaviour) and a slower baseline (`β_slow`, long-term behaviour).
- Detector: when `m_hat / m_slow_hat ≥ drift_threshold` (default 1.5×), the worker's `drift_factor` snaps to `drift_penalty` (default 5×). The factor multiplies the expected time-to-serve in the picker, deflecting traffic without ejecting.
- Recovery: when `m_hat / m_slow_hat ≤ drift_recovery_threshold` (default 1.1×), `drift_factor` returns to 1.0. Hysteresis prevents flapping.
- Health probes (Phase 1.1) feed successful-probe latencies back into the EMA, which lets a drifted worker that's since recovered earn its factor back without needing real traffic first.

**Measured on two Mac workers** (`scripts/bench_drift_detection.py`, softmax=0.05, drift_threshold=1.5):

| stage | duration | worker 0 picks | worker 1 picks | observation |
|---|---|---|---|---|
| baseline | 3 s | 26 (58 %) | 19 (42 %) | both at ratio ≈ 1.00 |
| drift injected (worker 0 += 250 ms/call) | 10 s | 8 (5 %) | 142 (95 %) | **drift detected at t+0.48 s**; traffic deflects |
| recovery (injection off) | 20 s | 167 (56 %) | 133 (44 %) | drift clears via softmax exploration + probe-fed EMA |

Result: from slowdown onset to ≥95 % traffic diversion in under half a second.

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

### 3.1 Checkpoint-handle queue — F5.1 ✅ (first slice)

Replace the "cloudpickle state_dict in callback" pattern with a **non-blocking checkpoint handle** flow. The first slice is shipped (`zakuro-ai/sakura#` — see below) — the state-dict copy now runs on a dedicated CUDA stream with an event; the main training thread returns without waiting for the PCIe transfer and the pool worker synchronises on the event before cloudpickling.

**Profiled on x399 4090, distilbert (268 MB fp32 state_dict), 5-epoch fine-tune:**

| step | blocking `.cpu()` (previous) | async CUDA-stream copy (new) |
|---|---|---|
| `model.state_dict()` | 0.4 ms | 0.4 ms |
| GPU→CPU transfer on main thread | **172 ms** | — (offloaded to copy stream) |
| dict comprehension + event record | — | **75 ms** (CPU-side refs only) |
| cloudpickle.dumps (pool thread) | 364 ms | 364 ms |
| **main-thread total** | **172 ms/epoch** | **75 ms/epoch** |

**Measured savings: 101.3 ms per epoch, 57.5 % reduction** on the main training thread. Per 15-epoch fine-tune: 1.5 s of training time reclaimed.

**Pool-thread improvement (sakura #39):** the same state_dict blob used to get `cloudpickle.dumps`d (481 ms, 39 % GIL-share). Switched to `torch.save(sd, BytesIO)` — **282 ms, 72 % GIL-share**. Training step now has roughly 2× the CPU during the pool's packaging window.

Next-up slices:
- In-memory `torch.Tensor` views as handle type (zero-copy on same host).
- Disk-path handles written by a dedicated CUDA stream (GPU → local SSD, non-blocking).
- Object-store URIs (for multi-machine).

The training loop never owns a cloudpickle operation; the evaluator dispatcher does. ✅

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
