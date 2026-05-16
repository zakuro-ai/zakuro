![zakuro Logo](imgs/zakuro-banner.png)

--------------------------------------------------------------------------------

<p align="center">
        <img alt="License" src="https://img.shields.io/github/license/zakuro-ai/zakuro.svg?color=blue">
        <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#installation">Installation</a> •
  <a href="#concepts">Concepts</a> •
  <a href="#adaptive-compute">Adaptive Compute</a> •
  <a href="#notebooks">Notebooks</a> •
  <a href="#benchmarks">Benchmarks</a> •
  <a href="#docs">Docs</a>
</p>

**Zakuro is a context-aware distributed-ML runtime.** You decorate a Python function, declare a pool of workers, and the framework routes each call to the worker with the lowest expected time-to-serve — learning from every dispatch, reacting to node failures and performance drift, and never making training wait on things that can be decoupled.

See [`PRD.md`](PRD.md) for the product vision and [`PLAN.md`](PLAN.md) for the measured engineering progress (every number observed, nothing simulated).

## Quick start

### Run a function on a remote worker — three lines

```python
import zakuro as zk

@zk.fn
def square(x: int) -> int:
    return x * x

# Run on a remote worker (QUIC transport, fast).
result = square.to(zk.Compute(uri="quic://worker:4433"))(7)  # → 49
```

No worker? Zakuro falls back to running the function **in-process** so your code keeps working:

```python
# No `uri`, no `host` → standalone fallback; the call runs locally.
result = square.to(zk.Compute())(7)  # → 49
```

### Adapt across a pool — Adam-style allocator

```python
import zakuro as zk

workers = [zk.Worker.spawn(name=f"w{i}") for i in range(3)]
adaptive = zk.AdaptiveCompute(
    workers=[w.compute(verify=False) for w in workers],
    beta1=0.9,
    softmax_temperature=0.02,  # exploration; set 0 for greedy argmin
)
adaptive.warmup(rounds=3)                # auto-calibrate per-worker priors
adaptive.start_health_probes(interval=0.5, max_strikes=2)  # detect drop-outs

@zk.fn
def expensive(x): ...

# The allocator picks the worker with the lowest expected time-to-serve,
# tracks per-worker latency EMA + variance, soft-demotes drifted workers,
# suspends failed ones.
result = expensive.to(adaptive)(42)
```

## Installation

### From source (recommended while the 0.3 series is pre-release)

```bash
git clone https://github.com/zakuro-ai/zakuro
cd zakuro
uv sync --extra worker         # pulls FastAPI + uvicorn + aioquic for the worker CLI
```

### From PyPI

```bash
# Core only (enough to be a client):
pip install zakuro-ai

# Core + worker (needed to run `zakuro-worker`):
pip install 'zakuro-ai[worker]'
```

### Optional extras

| extra | adds |
|---|---|
| `[worker]` | FastAPI, uvicorn, aioquic, psutil — needed to **run** a worker |
| `[ray]` | Ray processor |
| `[dask]` | Dask processor |
| `[spark]` | PySpark processor |
| `[dev]` | pytest, ruff, mypy |

You **do not** need `[worker]` just to call into a running worker — `import zakuro` stays lean on purpose.

### `zc` CLI (optional)

`zc` is the Rust broker + CLI at [zakuro-ai/zc](https://github.com/zakuro-ai/zc). It's useful for multi-node meshes but not required for a single-process or small-cluster setup.

```bash
# macOS / Linux one-liner — installs to /usr/local/bin
curl -sSL https://raw.githubusercontent.com/zakuro-ai/zc/master/scripts/install.sh | bash
```

## Concepts

### `Compute`

A target for remote execution.

| How it's constructed | Where the call runs |
|---|---|
| `zk.Compute()` | **Standalone** — in-process fallback. No network, no workers required. |
| `zk.Compute(uri="quic://host:4433")` | QUIC worker. Probed at construction; raises `ConnectionError` if unreachable. |
| `zk.Compute(uri="zakuro://host:3960")` | HTTP worker. Same probe behaviour. |
| `zk.Compute(uri="ray://head:10001")` | Ray cluster (requires `[ray]`). |

Resource hints: `cpus=`, `gpus=`, `memory=`. All advisory on standalone; **an explicit `memory=` refuses to run in standalone** because we can't enforce it.

### `@zk.fn` / `@zk.cls`

Decorators that turn a function or class into something remotely callable. Under the hood the callable is cloudpickled and shipped to the worker on each call; `zk.cls` keeps the instance alive on a specific worker for subsequent method calls.

### `zk.Worker.spawn()`

Convenience wrapper around the `zakuro-worker` CLI. Takes `name=`, `transport="http"|"quic"`, `port=` (ephemeral if omitted). Polls `/health` until ready, registers an `atexit` hook so stray subprocesses die with the calling Python.

### Transports

| scheme | default port | transport | when to use |
|---|---|---|---|
| `zakuro://` | 3960 | HTTP (FastAPI + httpx) | interop with any load-balancer / reverse proxy |
| `quic://` | 4433 | QUIC (aioquic) | fastest; connection-level resilience built in |
| `ray://` | 10001 | Ray | existing Ray cluster |
| `dask://` / `tcp://` | 8786 | Dask | existing Dask scheduler |
| `spark://` | 7077 | Spark | existing Spark master |

QUIC is the default for new work. It handles worker bounces natively (retry + 5 s idle timeout instead of aioquic's 30–60 s default — measured 12× faster detection).

## Adaptive compute

`zk.AdaptiveCompute` is the core differentiator. It tracks per-worker latency with Adam-style EMAs (fast + slow baseline), variance, queue depth, health-probe outcomes, and failure counts — and picks the worker with the lowest expected time-to-serve for every dispatch.

### What it does, observed

All numbers come from real subprocess workers running on the Mac (see `scripts/bench_*.py`):

| Feature | Measured behaviour |
|---|---|
| **Warmup** | Auto-derives `backpressure_threshold = 1.5 × max(worker p95)` — `29 ms` on the 3-worker Mac mesh. No manual tuning. |
| **Greedy vs softmax routing** | Greedy commits to the 3-ms-faster worker (100 %/0 %). Softmax `τ=0.02` keeps all three workers utilised (172/152/176). |
| **Add / remove workers at runtime** | Removed worker drops to 0 picks within the batch; readmitted worker starts at mesh-median prior and earns traffic immediately. |
| **Health probes** | Background thread detects SIGKILL in **18 ms** (tight config) / 743 ms (loose). Worker suspended; traffic reroutes. |
| **Drift detection** | Injected 250 ms/call slowdown ⇒ 95 % traffic diversion at **t + 0.48 s**. Recovery via softmax + health-probe latencies. |
| **QUIC retry** | In-flight request during worker SIGKILL surfaces `ConnectionError` in **5 s** (vs aioquic's 30–60 s default). |

Full numbers in [`PLAN.md`](PLAN.md#measured-results-so-far).

## Notebooks

Each one runs end-to-end on a laptop and prints observed numbers — no faked data, no simulated failures. Verified by `jupyter nbconvert --execute`.

| notebook | path | what it shows |
|---|---|---|
| **Standalone** | [`notebooks/standalone_mode.ipynb`](notebooks/standalone_mode.ipynb) | `Compute()` without a URI → in-process fallback; advisory resource hints; memory-enforcement refusal |
| **Two workers** | [`notebooks/two_worker_demo.ipynb`](notebooks/two_worker_demo.ipynb) | Spawn two workers via `zk.Worker.spawn()`, chain calls between them |
| **Mesh adaptation tour** | [`notebooks/mesh_adaptation_tour.ipynb`](notebooks/mesh_adaptation_tour.ipynb) | Every `AdaptiveCompute` knob — warmup, soft/greedy, add/remove, health, drift |
| **QUIC resilience** | [`notebooks/quic_resilience.ipynb`](notebooks/quic_resilience.ipynb) | Baseline → SIGKILL → respawn; 5 s detection; post-respawn recovery |

The sakura repo adds [`bert_demo/hf_async_features.ipynb`](https://github.com/zakuro-ai/sakura/blob/master/bert_demo/hf_async_features.ipynb) — every `SakuraHFCallback` knob on a real BERT fine-tune.

## Benchmarks

All under `scripts/` and take a handful of seconds to a minute each. Output is JSON-dumpable via `--log <path>` where supported.

| script | scenario | headline |
|---|---|---|
| `bench_mesh_adaptation.py` | 2-worker warmup → dispatch → remove → readmit | `29 ms` auto-bp, rebalance within one batch |
| `bench_health_detection.py` | SIGKILL worker mid-run | `18–743 ms` detection depending on probe cadence |
| `bench_drift_detection.py` | Induce `sleep(0.25)` on one worker | drift detected `t + 0.48 s`, 95 % traffic diverted |
| `bench_quic_retry.py` | SIGKILL + respawn on same QUIC port | 60 s → 5 s dead-connection detection |
| `bench_all.py` | Runs the four above in one go | consolidated summary table |

```bash
uv run python scripts/bench_all.py --log /tmp/zakuro-bench.json
```

## Docs

- [`PRD.md`](PRD.md) — product vision (what we're building and why)
- [`PLAN.md`](PLAN.md) — engineering plan (what's shipped with measured numbers)
- [`docs/getting-started.md`](docs/getting-started.md) — end-to-end guide, "laptop-only" and "networked" paths
- [`docs/cli.md`](docs/cli.md) — `zakuro-worker` CLI reference
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — QUIC wire protocol (so new bindings can implement against it)
- [`docs/zc-quic-patch/`](docs/zc-quic-patch/) — Rust broker-side QUIC caller, shipped at [zakuro-ai/zc#31](https://github.com/zakuro-ai/zc/pull/31)

## Related projects

- [**sakura**](https://github.com/zakuro-ai/sakura) — ML framework integrations (PyTorch Lightning, HuggingFace Trainer, TensorFlow/Keras) that use Zakuro under the hood to hide eval latency behind training
- [**zc**](https://github.com/zakuro-ai/zc) — Rust broker with QUIC transport, credit-based billing, P2P mesh

## Development

```bash
# Tests
uv run pytest tests/

# Specific benchmark
uv run python scripts/bench_mesh_adaptation.py --n-workers 3

# Build a wheel
task build:wheel

# Full CI set
task ci:all
```

Python 3.10+ is required. `uv` is the recommended package manager (`pip install uv`).

## Container images

The worker is published in two variants, both built for `linux/amd64` and `linux/arm64`:

| Tag suffix | Base image | Size | Use when |
|---|---|---|---|
| (none) — `:latest`, `:cpu` | `python:3.12-slim` | ~370 MB | Default. Has a shell + apt for debugging via `docker exec`. |
| `-distroless` — `:latest-distroless` | `gcr.io/distroless/python3-debian12:nonroot` | ~155 MB | Production. No shell, no apt, runs as the distroless `nonroot` user (uid 65532). Minimal CVE surface. |

Pull from either Docker Hub or GHCR:

```bash
docker pull zakuroai/zakuro-worker:latest          # slim
docker pull zakuroai/zakuro-worker:latest-distroless

docker pull ghcr.io/zakuro-ai/zakuro-worker:latest
docker pull ghcr.io/zakuro-ai/zakuro-worker:latest-distroless
```

Both variants run the same FastAPI worker on port `3960` with the same in-process `/health` healthcheck.

## Verifying releases

Every published wheel and container image is signed and attested. Detailed flow in [`docs/security/verifying-releases.md`](docs/security/verifying-releases.md); the one-liners are:

```bash
# Wheel — SLSA L3 provenance
slsa-verifier verify-artifact \
    --provenance-path zakuro_ai-X.Y.Z-py3-none-any.whl.intoto.jsonl \
    --source-uri github.com/zakuro-ai/zakuro \
    --source-tag vX.Y.Z \
    zakuro_ai-X.Y.Z-py3-none-any.whl

# Container image — Cosign keyless (signature lives in Rekor)
cosign verify ghcr.io/zakuro-ai/zakuro-worker:X.Y.Z \
    --certificate-identity-regexp '^https://github\.com/zakuro-ai/zakuro/\.github/workflows/publish\.yml@refs/tags/.*$' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

If verification fails on what appears to be a real release, **do not run the artefact** — report to `security@zakuro.ai`.

## License

BSD-3-Clause. See [`LICENSE`](LICENSE).
