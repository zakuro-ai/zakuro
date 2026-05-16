# Getting started with Zakuro

This guide walks you from a fresh Python install to training a real model with Zakuro + Sakura. Two tracks:

- **[Track A — laptop only](#track-a--laptop-only-no-workers-needed)**: no workers, no network access to `zakuro-ai.com`, no `zc` CLI. Everything runs in-process. Perfect for getting a feel for the API, running the notebooks, CI, demos.
- **[Track B — networked mesh](#track-b--networked-mesh)**: spawn one or more `zakuro-worker` processes (on your laptop or across machines), dispatch work over HTTP or QUIC, get real parallelism. Optional `zc` broker for P2P.

Every command is copy-paste-able. Every Python snippet actually runs.

---

## Prerequisites

- **Python 3.10+** (3.10, 3.11, 3.12 all tested).
- **[`uv`](https://github.com/astral-sh/uv)** for package management — `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`. Anything `uv` does, regular `pip` also does; the docs use `uv` because it's faster.
- **~2 GB free disk** for PyTorch + transformers + sakura (only if you plan to train real models).

No account, no API key, no network access to `zakuro-ai.com` is needed for Track A. Track B only needs that if you want to use the hosted broker.

---

## Track A — laptop only (no workers needed)

### Step 1 — install

```bash
mkdir my-zakuro && cd my-zakuro
uv venv
source .venv/bin/activate
uv pip install zakuro-ai
```

That's it. Core Zakuro is lean — no FastAPI, no aioquic, no torch. Just enough to use the decorators and the standalone fallback.

### Step 2 — first remote call (runs in-process)

Save as `hello.py`:

```python
import zakuro as zk

@zk.fn
def greet(name: str) -> str:
    return f"Hello from Zakuro, {name}!"

# zk.Compute() with no URI → Zakuro detects no backend and falls back
# to in-process execution. No worker, no network, no config needed.
print(greet.to(zk.Compute())("world"))
```

```bash
python hello.py
# → Hello from Zakuro, world!
```

The call is routed through `@zk.fn`'s dispatch machinery but ultimately runs in this same Python process.

### Step 3 — AdaptiveCompute with a single in-process worker

```python
import zakuro as zk

adaptive = zk.AdaptiveCompute(workers=[zk.Compute()])   # one virtual worker
adaptive.warmup(rounds=3, verbose=True)                 # calibrates priors

@zk.fn
def square(x): return x * x

results = [square.to(adaptive)(i) for i in range(10)]
print(results)
for s in adaptive.stats():
    print(f"  ema={s['latency_ema']*1000:.2f}ms  step={s['step']}")
```

You now have the full AdaptiveCompute instrumentation — latency EMAs, queue depth, warmup-derived backpressure — against a single in-process worker. Useful as a testbed for writing training code before any real cluster exists.

### Step 4 — training a model (Sakura + Lightning)

```bash
uv pip install 'sakura-ml[huggingface]' lightning torchvision
```

Save as `mnist_demo.py` (short version; see `sakura/main.py` for the full one):

```python
import lightning as L
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import MNIST

from sakura.lightning import SakuraTrainer


class MNISTModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.l1 = nn.Linear(28 * 28, 128)
        self.l2 = nn.Linear(128, 10)

    def forward(self, x):
        return self.l2(F.relu(self.l1(x.view(x.size(0), -1))))

    def training_step(self, batch, _):
        x, y = batch
        return F.cross_entropy(self(x), y)

    def validation_step(self, batch, _):
        return F.cross_entropy(self(*batch[:1]), batch[1])

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)


if __name__ == "__main__":
    tfm = transforms.ToTensor()
    train = DataLoader(MNIST(".", train=True, download=True, transform=tfm), batch_size=64)
    val = DataLoader(MNIST(".", train=False, transform=tfm), batch_size=256)

    trainer = SakuraTrainer(
        max_epochs=3, accelerator="auto",
        model_factory=MNISTModel, val_loader_factory=lambda: val,
    )
    trainer.run(MNISTModel(), train, val)
    print("history:", trainer.history)
```

```bash
python mnist_demo.py
```

Even without a worker, training overlaps eval via Sakura's in-process async pattern — no MPI, no Redis, no `SAKURA_ROLE` to set.

### Step 5 — try the notebooks

```bash
uv pip install jupyter
jupyter notebook
```

Browse to `notebooks/` in the `zakuro` repo (or `bert_demo/` in `sakura`). The notebooks are executable end-to-end without any worker setup.

---

## Track B — networked mesh

When you outgrow single-process execution: spawn real workers, dispatch over HTTP or QUIC, let `AdaptiveCompute` route across them.

### Step 1 — install with the worker extra

```bash
uv pip install 'zakuro-ai[worker]'
```

This adds FastAPI, uvicorn, aioquic, psutil — everything `zakuro-worker` needs to actually *serve* calls.

Verify the CLI:

```bash
zakuro-worker --help
```

### Step 2 — run a worker

#### Option A: HTTP transport (portable)

```bash
# In terminal 1
zakuro-worker --host 0.0.0.0 --port 3960 --worker-name w1
```

#### Option B: QUIC transport (fastest; recommended)

```bash
zakuro-worker --transport quic --host 0.0.0.0 --port 4433 --worker-name w1
```

Or spawn programmatically from Python — `zk.Worker.spawn()` runs the same CLI as a subprocess and polls `/health`:

```python
import zakuro as zk

with zk.Worker.spawn(name="w1", transport="quic") as worker:
    print(worker.uri)         # quic://127.0.0.1:<ephemeral-port>
    # ... dispatch work ...
# subprocess stops automatically on context exit
```

### Step 3 — dispatch across workers with `AdaptiveCompute`

```python
import zakuro as zk

workers = [
    zk.Worker.spawn(name=f"w{i}", transport="quic")
    for i in range(3)
]

adaptive = zk.AdaptiveCompute(
    workers=[w.compute(verify=False) for w in workers],
    beta1=0.9,                   # responsive to recent latency
    beta_slow=0.995,             # slow baseline for drift detection
    softmax_temperature=0.02,    # 0 for greedy argmin; >0 for exploration
)

adaptive.warmup(rounds=3)                                 # seed priors from real probes
adaptive.start_health_probes(interval=0.5, max_strikes=2) # background liveness

@zk.fn
def work(x): return x * 2

print([work.to(adaptive)(i) for i in range(50)])
print(adaptive.stats())

adaptive.stop_health_probes()
for w in workers:
    w.stop()
```

### Step 4 — training across machines

The simplest cross-machine setup: **training node + eval node**.

On the **eval machine** (Mac, weaker GPU, CI box, etc.):

```bash
zakuro-worker --transport quic --host 0.0.0.0 --port 4433 --worker-name eval-node
```

On the **training machine** (the GPU box):

```python
import zakuro as zk
from transformers import Trainer, TrainingArguments, AutoModelForSequenceClassification
from sakura.huggingface import SakuraHFCallback

trainer = Trainer(
    model=my_model,
    args=TrainingArguments(..., eval_strategy="no"),  # Sakura handles eval
    train_dataset=train,
    callbacks=[
        SakuraHFCallback(
            model_factory=lambda: AutoModelForSequenceClassification.from_config(config),
            eval_fn=my_eval_fn,
            eval_payload=(val_payload, 32),
            val_compute=zk.Compute(uri="quic://eval-node.example.com:4433"),
            on_backpressure="skip",    # drop an epoch's eval if the mesh is saturated
            fp16_state_dict=True,      # halve the wire bytes
        )
    ],
)
trainer.train()
```

Training never blocks on eval; the callback hands the state_dict to the eval worker and reaps the result when it's ready. If the eval machine can't keep up, backpressure kicks in and training proceeds without that epoch's metric.

### Step 5 — the broker (optional, for multi-tenant / many workers)

If you're running ≥ 4 workers across machines, or want credit-based billing / multi-tenant routing, add the **`zc`** broker:

```bash
# install zc (Rust binary)
curl -sSL https://raw.githubusercontent.com/zakuro-ai/zc/master/scripts/install.sh | bash

# start a broker on port 9000 with live transaction log
zc broker
```

Workers register with the broker via Tailscale discovery or explicit `ZAKURO_PEERS`. Clients target the broker by URI:

```python
compute = zk.Compute(uri="zc://broker.local:9000", cpus=4)
```

The broker picks the best worker using its routing strategy (`best_price`, `best_latency`, `round_robin`, etc.). Zakuro's `AdaptiveCompute` sits above the broker for additional client-side smarts; the two compose cleanly.

### Step 6 — Tailscale + hosted broker (optional)

Zakuro's production mesh uses Tailscale for worker discovery and the hosted broker at `my.zakuro-ai.com` for billing:

```bash
# Set up Tailscale on each host that will run a worker
sudo tailscale up --authkey=tskey-auth-...

# Start a worker — it'll advertise itself on the tailnet
ZAKURO_WORKER_TAGS=gpu,a100 zakuro-worker --transport quic

# On the client (also on the tailnet):
compute = zk.Compute(uri="zc://my.zakuro-ai.com:9000")
```

This path needs a Zakuro account for the broker API key (`$ZAKURO_AUTH`). Everything else works locally without an account.

---

## Troubleshooting

### `import zakuro` fails with `ModuleNotFoundError: fastapi`

You installed `zakuro-ai` without the `[worker]` extra and something (probably `zakuro-worker`) is importing server code. Fix:

```bash
uv pip install 'zakuro-ai[worker]'
```

Note: `import zakuro` itself is guaranteed not to need `[worker]` — if you hit this error, you're probably running the CLI.

### `Compute(memory="2Gi")()` raises in standalone

By design. Zakuro can't enforce memory limits in-process, so an explicit `memory=` plus standalone fallback raises `RuntimeError`. Either start a real worker, or drop the `memory=` hint.

### `zakuro-worker: command not found` inside a notebook

`zk.Worker.spawn()` locates the CLI relative to `sys.executable` first, then falls back to `$PATH`. If your Python is a venv but the CLI isn't installed in that venv, install it:

```bash
.venv/bin/pip install 'zakuro-ai[worker]'
```

### QUIC dispatch hangs for ~60 s on worker crash

You're on an old zakuro (< 0.2.3). Upgrade — the current version sets `idle_timeout=5.0` on the QUIC client, detecting drops 12× faster.

---

## Next steps

- [`PLAN.md`](https://github.com/zakuro-ai/zakuro/blob/master/PLAN.md) — every shipped feature with a measured number next to it.
- [`PRD.md`](https://github.com/zakuro-ai/zakuro/blob/master/PRD.md) — the product vision and principles.
- [`docs/cli.md`](cli.md) — full `zakuro-worker` CLI reference.
- [`docs/PROTOCOL.md`](PROTOCOL.md) — QUIC wire protocol, in case you want to write a binding in another language.
- Notebooks in the repo — `notebooks/mesh_adaptation_tour.ipynb`, `notebooks/quic_resilience.ipynb`, and `notebooks/standalone_mode.ipynb` are all executable end-to-end.
