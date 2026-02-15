![zakuro Logo](imgs/zakuro-banner.png)

--------------------------------------------------------------------------------

<p align="center">
        <img alt="Build" src="https://github.com/zakuro-ai/zakuro/actions/workflows/test.yml/badge.svg?branch=main">
        <img alt="GitHub" src="https://img.shields.io/github/license/zakuro-ai/zakuro.svg?color=blue">
        <img alt="GitHub release" src="https://img.shields.io/github/release/zakuro-ai/zakuro.svg">
        <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#installation">Installation</a> •
  <a href="#modules">Modules</a> •
  <a href="#development">Development</a>
</p>

Zakuro is a distributed computing library that makes running Python functions on remote workers as simple as adding a decorator. Inspired by [kubetorch](https://github.com/run-house/kubetorch), it provides a clean Pythonic API for cluster computing.

## Quick Start

```python
import zakuro as zk

def hello_world():
    return "Hello from Zakuro!"

# Define compute resources
compute = zk.Compute(cpus=0.5, memory="2Gi")

# Send function to remote worker
remote_fn = zk.fn(hello_world).to(compute)

# Execute remotely
result = remote_fn()  # Returns "Hello from Zakuro!"
```

## Features

- **Simple decorator-based API** - Use `@zk.fn` to make any function remotely executable
- **Resource specification** - Define CPU, memory, and GPU requirements with `zk.Compute`
- **Multiple backends** - Supports HTTP, Ray, Dask, and Spark via URI-based selection
- **Closure support** - Closures and lambdas work transparently via cloudpickle
- **Remote classes** - Use `@zk.cls` for remote class instantiation
- **Automatic discovery** - Workers discovered via Tailscale or DNS
- **Backend auto-detection** - Automatically discovers available processors

## Installation

```bash
# Install from PyPI
pip install zakuro-ai

# Or with uv
uv pip install zakuro-ai
```

### Optional Processor Backends

```bash
# Install with Ray support
pip install "zakuro-ai[ray]"

# Install with Dask support
pip install "zakuro-ai[dask]"

# Install with Spark support
pip install "zakuro-ai[spark]"

# Install all processors
pip install "zakuro-ai[all-processors]"
```

For development:
```bash
# Clone the repository
git clone https://github.com/zakuro-ai/zakuro
cd zakuro

# Install with dev dependencies
uv pip install -e ".[dev]"
```

## Modules

| Component | Description |
| --------- | ----------- |
| **zakuro.compute** | Resource specification (CPU, memory, GPU) |
| **zakuro.fn** | Function and class decorators |
| **zakuro.processors** | Backend processors (HTTP, Ray, Dask, Spark) |
| **zakuro.client** | HTTP client for worker communication |
| **zakuro.config** | Configuration management |
| **zakuro.discovery** | Worker discovery via Tailscale/DNS |
| **zakuro.worker** | FastAPI-based worker server |
| **zakuro.fs** | MinIO/ZFS filesystem integration |
| **zakuro.hub** | Model hub for pretrained models |

## API Reference

### Compute

Define compute resources for remote execution:

```python
import zakuro as zk

# Basic compute target (uses HTTP backend by default)
compute = zk.Compute(
    cpus=2.0,           # CPU cores
    memory="4Gi",       # Memory (supports Gi, Mi, G, M)
    gpus=1,             # GPU count
    host="worker.local", # Worker host (optional, auto-discovered)
    port=3960,          # Worker port
    env={"KEY": "val"}, # Environment variables
)

# URI-based backend selection
compute = zk.Compute(uri="ray://head:10001", cpus=4)
compute = zk.Compute(uri="dask://scheduler:8786", memory="8Gi")
compute = zk.Compute(uri="spark://master:7077", gpus=1)
compute = zk.Compute(uri="zakuro://worker:3960")  # HTTP backend
```

### Processor Backends

Zakuro supports multiple compute backends via URI-based selection:

| URI Scheme | Backend | Priority | Default Port | Install |
| ---------- | ------- | -------- | ------------ | ------- |
| `zakuro://` | HTTP (default) | 10 | 3960 | included |
| `spark://` | Apache Spark | 30 | 7077 | `[spark]` |
| `dask://` | Dask Distributed | 40 | 8786 | `[dask]` |
| `ray://` | Ray | 50 | 10001 | `[ray]` |
| `zc://` | P2P Broker | 100 | 9000 | included |

```python
import zakuro as zk

# Check available processors
print(zk.available_processors())  # ['zakuro', 'ray', 'dask', 'spark']

# Use Ray backend
compute = zk.Compute(uri="ray://ray-head:10001", cpus=4, memory="8Gi")
result = zk.fn(my_func).to(compute)()

# Use Dask backend
compute = zk.Compute(uri="dask://scheduler:8786", cpus=2)
result = zk.fn(my_func).to(compute)()

# Use Spark backend
compute = zk.Compute(uri="spark://master:7077", memory="4Gi")
result = zk.fn(my_func).to(compute)()

# Use P2P Broker (optimal worker selection with credit billing)
compute = zk.Compute(uri="zc://broker:9000", cpus=4, memory="8Gi")
result = zk.fn(my_func).to(compute)()
```

### P2P Compute Broker

The broker provides optimal worker routing with credit-based billing:

```python
import zakuro as zk

# Connect to broker for P2P compute
compute = zk.Compute(
    uri="zc://broker.tailnet:9000",
    cpus=4,
    memory="8Gi",
    processor_options={"user_id": "my-user"}
)

# Function is routed to optimal worker based on:
# - Resource availability
# - Price per compute (CPU/memory/GPU)
# - Worker latency and load
result = zk.fn(my_func).to(compute)()
```

Start a broker with the `zc` CLI:
```bash
zc broker              # Foreground with live transaction log
zc broker 8080         # Custom port
zc -d broker           # Daemon mode (background)
```

#### Credit Management

The broker uses a Redis-based ledger with API key authentication:

```bash
# Add credits (requires API key)
curl -X POST http://broker:9000/credits/my-user/add \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $ZAKURO_MASTER_KEY" \
  -d '{"amount": 100.0}'

# Check balance
curl http://broker:9000/credits/my-user
```

#### Discovery Modes

| Mode | Trigger | Cost |
|------|---------|------|
| **Tailscale** | VPN network detected | Per-resource pricing |
| **Local** | No Tailscale | Free (development) |

### Function Decorator

Make functions remotely executable:

```python
import zakuro as zk

@zk.fn
def compute_heavy(x: int) -> int:
    return x ** 2

# Run locally
result = compute_heavy(10)  # 100

# Run remotely
compute = zk.Compute(cpus=4, memory="8Gi")
result = compute_heavy.to(compute)(10)  # 100, executed on worker
```

### Class Decorator

Remote class instantiation:

```python
import zakuro as zk

@zk.cls
class Model:
    def __init__(self, name: str):
        self.name = name

    def predict(self, x):
        return f"{self.name}: {x * 2}"

# Create remote instance
compute = zk.Compute(gpus=1)
model = Model.to(compute)("my-model")
result = model.predict(10)  # "my-model: 20"
```

## Development

### Prerequisites

- Python 3.10+
- [Task](https://taskfile.dev) (build automation)
- [uv](https://github.com/astral-sh/uv) (package management)

### Commands

```bash
# Run tests
task test:unit

# Run linting
task ci:lint

# Format code
task ci:format

# Build wheel
task build:wheel

# Run all CI checks
task ci:all
```

### Docker

```bash
# Build worker image
task docker:build

# Start worker
task docker:up

# View logs
task docker:logs

# Stop worker
task docker:down
```

### P2P Mesh Deployment

Deploy a 2-node mesh where each node runs a broker + worker pair connected via Tailscale. Local execution is free; remote execution is charged via the credit ledger.

```
  Node 1 (standard)                          Node 2 (premium)
  ┌──────────────────────┐                   ┌──────────────────────┐
  │ Broker + Worker      │◄══ Tailscale ════►│ Broker + Worker      │
  │ + Tailscale + Redis  │    mesh           │ + Tailscale + Redis  │
  │ cpu=$0.001/s (free)  │                   │ cpu=$0.003/s (free)  │
  └──────────────────────┘                   └──────────────────────┘
```

```bash
cd docker

# Set Tailscale auth keys
export ZK0NODE01_API_KEY=tskey-auth-...
export ZK0NODE02_API_KEY=tskey-auth-...

# Set Tailscale IPs (from `tailscale ip -4` on each node)
export NODE1_TAILSCALE_IP=100.x.x.x
export NODE2_TAILSCALE_IP=100.y.y.y

# Start the mesh (8 containers: 2x broker + worker + tailscale + redis)
docker compose -f docker-compose.mesh.yml up -d --build

# Verify both nodes see 2 workers
curl http://localhost:9001/workers   # node1 broker
curl http://localhost:9002/workers   # node2 broker

# Run the demo (tests all routing strategies)
pip install cloudpickle requests
python mesh-demo.py http://localhost:9001 node1-user
```

The demo sends real cloudpickle-serialized Python functions through the broker:
- `best_price` picks the local worker (free, cost=0)
- `round_robin` alternates between local and remote (remote is charged)
- `best_latency` picks based on response time history

See `docker/docker-compose.mesh.yml` for the full compose configuration.

### Development Server

```bash
# Start development worker with hot reload
docker compose --profile dev up zakuro-worker-dev
```

## Configuration

Zakuro can be configured via environment variables:

### Worker Configuration

| Variable | Description | Default |
| -------- | ----------- | ------- |
| `ZAKURO_HOST` | Default worker host | `127.0.0.1` |
| `ZAKURO_PORT` | Default worker port | `3960` |
| `ZAKURO_URI` | Default processor URI | `zakuro://127.0.0.1:3960` |
| `ZAKURO_AUTH` | Authentication token | - |
| `ZAKURO_WORKER_NAME` | Worker name for discovery | `worker-{hostname}` |
| `ZAKURO_WORKER_TYPE` | Worker type identifier | `zakuro` |
| `ZAKURO_CPU_PRICE` | Price per CPU-second | `0.001` |
| `ZAKURO_MEMORY_PRICE` | Price per GiB-second | `0.0001` |
| `ZAKURO_GPU_PRICE` | Price per GPU-second | `0.01` |
| `TAILSCALE_AUTHKEY` | Tailscale auth key | - |

### Broker Configuration

| Variable | Description | Default |
| -------- | ----------- | ------- |
| `ZAKURO_MASTER_KEY` | Master API key for admin operations | - |
| `REDIS_URL` | Redis connection for ledger | `redis://127.0.0.1:6379` |

## License

BSD-3-Clause
