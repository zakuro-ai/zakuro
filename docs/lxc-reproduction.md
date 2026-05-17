# Reproducing Zakuro's benchmarks on an LXC GPU rig

Step-by-step walk-through for spinning up two LXC containers on a host that has `lxd` + `nvidia-container-toolkit` installed (Ubuntu 22.04+), installing Zakuro + Sakura, and reproducing every benchmark headline from the [runtime tracking board](https://github.com/orgs/zakuro-ai/projects/4) (formerly `PLAN.md`) **from a vanilla image**.

All numbers shown in the "Measured result" column below are the actual values observed when I ran this guide against a Threadripper x399 host with two GPUs. Your numbers will vary with CPU / GPU / network, but the ordering between configurations and the detection latencies should match within a factor of ~2.

## 0. Host prerequisites

- **LXD** (or Incus) ≥ 5.0. `snap install lxd --classic`.
- **NVIDIA drivers** installed on the host — `nvidia-smi` must work.
- A `gpu-profile` in LXD that exposes the GPU inside containers (sample below).

```bash
lxc profile create gpu-profile 2>/dev/null || true
lxc profile edit gpu-profile <<'EOF'
name: gpu-profile
description: ""
config:
  nvidia.runtime: "true"
devices:
  gpu0:
    type: gpu
EOF
```

## 1. Launch two containers

```bash
lxc launch ubuntu:22.04 zk-trainer   --profile default --profile gpu-profile
lxc launch ubuntu:22.04 zk-evaluator --profile default --profile gpu-profile
lxc list zk-   # should show both RUNNING + IPv4
```

**Measured result on the rig:**

```
+--------------+---------+-------------------+--
|     NAME     |  STATE  |       IPV4        |
+--------------+---------+-------------------+--
| zk-evaluator | RUNNING | 10.2.0.217 (eth0) |
| zk-trainer   | RUNNING | 10.2.0.159 (eth0) |
+--------------+---------+-------------------+--
```

## 2. Install Python + uv + zakuro in each container

Run the same block in both:

```bash
for c in zk-trainer zk-evaluator; do
  lxc exec "$c" -- bash -c '
    while pgrep apt >/dev/null; do sleep 2; done  # wait for cloud-init
    apt-get install -y -qq python3 python3-venv python3-pip curl
    curl -LsSf https://astral.sh/uv/install.sh | sh
    mkdir -p /opt/zk && cd /opt/zk
    uv venv --python 3.11 .venv
    uv pip install --python .venv/bin/python \
      "zakuro-ai[worker] @ git+https://github.com/zakuro-ai/zakuro.git@master" \
      "sakura-ml @ git+https://github.com/zakuro-ai/sakura.git@master"
  '
done
```

**Measured result:**

```
$ lxc exec zk-trainer -- /opt/zk/.venv/bin/python -c \
    "import zakuro, sakura; print(zakuro.__version__, sakura.__version__,
     hasattr(zakuro, 'AdaptiveCompute'))"
0.2.2 0.1.2 True
```

## 3. Download the benchmark scripts

```bash
lxc exec zk-trainer -- bash -c '
  cd /opt/zk
  for f in bench_mesh_adaptation.py bench_health_detection.py \
           bench_drift_detection.py bench_quic_retry.py; do
    curl -sL "https://raw.githubusercontent.com/zakuro-ai/zakuro/master/scripts/$f" -o "$f"
  done
'
```

## 4. Run each benchmark

All four run entirely inside `zk-trainer` (they spawn their own sub-workers). Replace `trainer` with whichever container you want if exercising cross-machine.

### 4.1 Mesh adaptation

```bash
lxc exec zk-trainer -- /opt/zk/.venv/bin/python /opt/zk/bench_mesh_adaptation.py \
  --n-workers 2 --dispatch 50 --log /tmp/mesh.json
```

**Measured result** (LXC, cold container, stock Ubuntu 22.04):

| stage | worker 0 picks | worker 1 picks | observation |
|---|---|---|---|
| after warmup | 2 (4 %) | 48 (96 %) | greedy commits to 4 ms-faster worker |
| after remove worker 0 | — | 50 (100 %) | trivial rebalance |
| after readmit | 37 (74 %) | 13 (26 %) | new worker's mesh-median prior earns traffic |

`warmup` recommended `backpressure_threshold = 0.03 s` from observed p95 latencies of 16–21 ms.

### 4.2 Health detection

```bash
lxc exec zk-trainer -- /opt/zk/.venv/bin/python /opt/zk/bench_health_detection.py \
  --duration 4 --kill-at 1.5 \
  --probe-interval 0.1 --probe-timeout 0.1 --max-strikes 1
```

**Measured result:**

```
[ 1.52s] SIGKILL worker 0 (pid 9778)
[ 1.52s] worker 0 suspended by health probe

detection latency: 17 ms
```

SIGKILL-to-suspend latency: **17 ms** — well under the PRD SM2 target of ≤ 10 s.

### 4.3 Drift detection

```bash
lxc exec zk-trainer -- /opt/zk/.venv/bin/python /opt/zk/bench_drift_detection.py \
  --baseline-secs 2 --injection-secs 6 --recovery-secs 2 \
  --rate 15 --drift-threshold 1.5 --softmax-temperature 0.05
```

**Measured result:**

| stage | worker 0 picks | worker 1 picks | drift |
|---|---|---|---|
| baseline 2 s | 19 | 11 | cleared |
| injection 6 s | 4 (4 %) | 86 (96 %) | **detected at t + 0.00 s**, drift_factor = 5 |
| recovery 2 s | 9 | 21 | cleared within 1 s |

### 4.4 QUIC retry

```bash
lxc exec zk-trainer -- bash -c '
  PATH=/opt/zk/.venv/bin:$PATH \
  /opt/zk/.venv/bin/python /opt/zk/bench_quic_retry.py --port 4476
'
```

**Measured result:**

```
baseline dispatch     :   149 ms
dispatch during kill  : 5 007 ms   (aioquic default idle_timeout would be 30–60 s)
dispatch post-respawn :   111 ms
```

Dead-connection detection: **5.0 s** after the 5 s idle_timeout we ship.

## 5. Cross-container dispatch — `zk-evaluator` serves, `zk-trainer` dispatches

Start a QUIC worker inside `zk-evaluator`, bind to `0.0.0.0` so the sibling container can reach it:

```bash
lxc exec zk-evaluator -- bash -c '
  nohup /opt/zk/.venv/bin/zakuro-worker \
    --transport quic --host 0.0.0.0 --port 4433 \
    --worker-name eval-lxc > /tmp/eval.log 2>&1 &
'
```

Sanity-check reachability:

```bash
lxc exec zk-trainer -- nc -uz 10.2.0.217 4433 && echo reachable
```

Dispatch from trainer to evaluator's worker:

```bash
lxc exec zk-trainer -- /opt/zk/.venv/bin/python - <<'PY'
import time
import zakuro as zk

compute = zk.Compute(uri="quic://10.2.0.217:4433", verify=False)

@zk.fn
def square(x): return x * x

t0 = time.perf_counter()
for i in range(10):
    assert square.to(compute)(i) == i * i
print(f"10 cross-container QUIC dispatches: {(time.perf_counter()-t0)*1000:.1f} ms")
PY
```

**Measured result:** 10 dispatches → **1 303.8 ms total** (≈ 130 ms / call, fresh connection per call; with a persistent processor in `AdaptiveCompute` this drops to tens of ms).

Stop the evaluator worker:

```bash
lxc exec zk-evaluator -- pkill -f "zakuro-worker.*4433"
```

## 6. Teardown

```bash
lxc stop zk-trainer zk-evaluator
lxc delete zk-trainer zk-evaluator
```

## Summary — measured numbers from one clean run

| Benchmark | Project-board headline | LXC observation |
|---|---|---|
| Mesh warmup bp | 29 ms on Mac | **30 ms** on Ubuntu LXC |
| Health SIGKILL→suspend | 18 ms (tight) | **17 ms** |
| Drift detection | 0.48 s | **<0.1 s** (faster — fresh Ubuntu glass) |
| QUIC dead-conn detection | 5 s vs 60 s default | **5.0 s** |
| Cross-container QUIC RPC | — | **~130 ms / call** (first call; drops with connection reuse) |

Every row was captured end-to-end in a fresh LXC container. If any of them move more than ~2× on your rig, open an issue with the captured JSON logs (`--log <path>` writes them).
