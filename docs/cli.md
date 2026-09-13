# Zakuro CLI reference

The `zakuro-worker` command starts a Zakuro worker: a FastAPI server that executes cloudpickled Python functions on behalf of remote callers. It is the only CLI shipped with this package; the higher-level `zc` CLI (broker, info, auth) lives in a separate project and is documented separately.

This reference covers:

1. [Installation and discovery](#installation-and-discovery)
2. [`zakuro-worker` options](#zakuro-worker-options)
3. [Environment variables](#environment-variables)
4. [HTTP endpoints exposed by the worker](#http-endpoints-exposed-by-the-worker)
5. [Lifecycle and signals](#lifecycle-and-signals)
6. [`python -m zakuro.worker`](#python--m-zakuroworker)
7. [Python wrapper: `zk.Worker`](#python-wrapper-zkworker)
8. [Common recipes](#common-recipes)
9. [Troubleshooting](#troubleshooting)

## Installation and discovery

`zakuro-worker` is registered as a console script in `pyproject.toml`:

```toml
[project.scripts]
zakuro-worker = "zakuro.worker.server:main"
```

After installing the package (`uv sync --all-extras`, `pip install zakuro-ai`, or equivalent), the binary is placed on `$PATH`:

```bash
which zakuro-worker
# /opt/code/ZAK/zakuro/.venv/bin/zakuro-worker
zakuro-worker --help
```

If the binary is missing, the package either isn't installed, or the venv isn't activated.

## `zakuro-worker` options

```
usage: zakuro-worker [-h] [--host HOST] [--port PORT] [--worker-name WORKER_NAME]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host HOST` | `$ZAKURO_HOST` or `0.0.0.0` | Bind address. Use `127.0.0.1` for loopback-only, `0.0.0.0` to accept external connections. |
| `--port PORT` | `$ZAKURO_PORT` or `3960` | TCP port to listen on. |
| `--worker-name NAME` | (none) | Sets `ZAKURO_WORKER_NAME` for the process, used in `/info` responses and exposed to functions via `os.environ`. |
| `-h, --help` | — | Show this help. |

Command-line flags always win over environment variables.

## Environment variables

The worker consults these at startup or inside request handlers. All are optional.

| Variable | Read by | Effect |
|----------|---------|--------|
| `ZAKURO_HOST` | startup | Default `--host` if the flag is omitted. |
| `ZAKURO_PORT` | startup | Default `--port` if the flag is omitted. |
| `ZAKURO_WORKER_NAME` | `/info`, execution env | Human-readable worker name; falls back to `worker-<hostname>`. |
| `ZAKURO_WORKER_TYPE` | `/info` | Label used by the broker for routing. Default `"zakuro"`. |
| `ZAKURO_WORKER_TAGS` | `/info` | Comma-separated tags advertised in `/info.tags`. |
| `ZAKURO_PRICE_PER_HOUR` | `/info.pricing` | Advertised hourly price, used by the broker's billing logic. Default `3.6`. |
| `ZAKURO_MIN_CHARGE` | `/info.pricing` | Minimum per-job charge. Default `0.001`. |

All environment variables seen by the worker process are available inside dispatched functions — the worker does not filter `os.environ`.

## HTTP endpoints exposed by the worker

Once running, the worker exposes the following endpoints. The only one a Zakuro client talks to during normal operation is `POST /execute`; the others are diagnostic.

| Method | Path | Purpose | Response |
|--------|------|---------|----------|
| GET | `/` | Service banner | `{"service": "Zakuro Worker", "version": "...", "docs": "/docs"}` |
| GET | `/health` | Liveness probe | `{"status": "healthy"}` (200) |
| GET | `/info` | Resource + pricing report for brokers | JSON — see below |
| POST | `/execute` | Execute a cloudpickled payload | Binary cloudpickle of the result |
| GET | `/docs` | FastAPI interactive docs | Swagger UI |

### `/info` payload

```json
{
  "name": "worker-B",
  "worker_type": "zakuro",
  "version": "0.2.0",
  "resources": {
    "cpus_total": 10,
    "cpus_available": 10,
    "memory_total": 17179869184,
    "memory_available": 9123840000,
    "gpus_total": 0,
    "gpus_available": 0
  },
  "hardware": {
    "cpu_model": "arm",
    "gpu_model": null,
    "gpu_vram_gb": null,
    "storage_gb": 241
  },
  "pricing": {
    "price_per_hour": 3.6,
    "min_charge": 0.001
  },
  "tags": []
}
```

GPU details come from `nvidia-smi` when present; memory comes from `psutil` when installed; otherwise sensible defaults are reported.

### `/execute` contract

- **Request body** (`application/octet-stream`): `cloudpickle.dumps({"func": callable, "args": tuple, "kwargs": dict})`.
- **Response body** (`application/octet-stream`): `cloudpickle.dumps(result_or_exception)`.
- **Status code**: always `200`. Exceptions raised by the user function are cloudpickled and returned in the body — the client is expected to `isinstance(result, Exception)` and re-raise.
- **Execution model**: the worker deserializes the payload and runs the function in a `ThreadPoolExecutor` sized to `multiprocessing.cpu_count()`. Threads share memory, so `@cls` instance state created via `/execute` with `action=create_instance` is visible to subsequent calls against the same instance ID.

## Lifecycle and signals

The worker runs `uvicorn.run(...)` with `reload=False`. Standard signal handling applies:

- `SIGTERM` / `SIGINT` → graceful shutdown: FastAPI `on_event("shutdown")` drains the thread pool (`executor.shutdown(wait=True)`), then the process exits.
- `SIGKILL` → immediate termination, no drain.

On startup the thread pool is initialized in `on_event("startup")`. Requests that arrive before startup completes receive `503 Worker not ready`.

There is no PID file, logrotate, or service definition; wrap the binary in systemd / launchd / supervisord / Docker for production deployments. The repo's `docker/` directory has reference compose files.

## `python -m zakuro.worker`

`python -m zakuro.worker` is equivalent to `zakuro-worker`; both enter `zakuro.worker.server:main`. Useful when you need a specific Python interpreter and want to skip the `$PATH` lookup:

```bash
python3.12 -m zakuro.worker --port 3960
```

Inside a venv, prefer the console script for clarity.

## Python wrapper: `zk.Worker`

For notebooks, tests, and scripts that need a transient worker, `zk.Worker.spawn()` wraps the CLI as a subprocess and polls `/health` until ready:

```python
import zakuro as zk

with zk.Worker.spawn(name="worker-B") as worker:
    print(worker.uri)                      # zakuro://127.0.0.1:54321
    print(worker.info()["resources"])      # same payload as /info
    result = zk.fn(lambda x: x * 2).to(worker.compute())(21)
    assert result == 42
# subprocess terminated on context exit
```

### Signature

```python
zk.Worker.spawn(
    name: str | None = None,     # sets ZAKURO_WORKER_NAME
    host: str = "127.0.0.1",     # bind address
    port: int | None = None,     # ephemeral if None
    timeout: float = 15.0,       # /health poll deadline
) -> zk.Worker
```

### Handle API

| Attribute | Description |
|-----------|-------------|
| `.uri` | `zakuro://<host>:<port>` — pass to `zk.Compute(uri=...)`. |
| `.host`, `.port`, `.name`, `.pid` | Direct accessors. |
| `.is_running` | `True` until the subprocess exits. |
| `.compute(**kwargs)` | Shortcut for `zk.Compute(uri=self.uri, **kwargs)`. |
| `.info()` | HTTP GET `/info`, returns parsed JSON. |
| `.stop(timeout=3.0)` | `SIGTERM`, then `SIGKILL` if the process doesn't exit within the timeout. |
| `__enter__` / `__exit__` | Context-manager form calls `.stop()` on exit. |
| `atexit` hook | All live `Worker` handles are stopped when the Python process exits, so orphaned subprocesses don't outlive the kernel. |

`zk.Worker.spawn` raises:

- `RuntimeError("zakuro-worker CLI not found on PATH...")` — the binary isn't discoverable.
- `RuntimeError("worker ... exited early (rc=...)")` — the subprocess died before becoming healthy (usually a port collision or a crash during startup).
- `TimeoutError("worker ... did not become healthy within Xs")` — `/health` never returned 200 inside the timeout window.

## Common recipes

### Run one worker in the foreground

```bash
zakuro-worker --host 127.0.0.1 --port 3960 --worker-name dev
```

### Run two workers on different ports

```bash
ZAKURO_WORKER_NAME=a zakuro-worker --port 3960 &
ZAKURO_WORKER_NAME=b zakuro-worker --port 3961 &
```

Then from any Python process:

```python
import zakuro as zk
result = zk.fn(lambda x: x ** 2).to(zk.Compute(uri="zakuro://127.0.0.1:3961"))(7)
```

### Expose a worker over the mesh / remote network

```bash
ZAKURO_WORKER_NAME=gpu-01 ZAKURO_WORKER_TAGS=gpu,a100 \
  zakuro-worker --host 0.0.0.0 --port 3960
```

Clients reach it via `zakuro://<mesh-ip>:3960` (or whatever FQDN/IP resolves).

### Dockerized worker

See `docker/Dockerfile` in this repo. The container's entrypoint is `zakuro-worker`, port 3960 is `EXPOSE`d, and the relevant env vars are wired through `docker/compose.*.yml`.

### Sanity-check a live worker

```bash
curl -s http://127.0.0.1:3960/health
# {"status":"healthy"}
curl -s http://127.0.0.1:3960/info | jq .resources
```

### Verify reachability from Python without dispatching

```python
import zakuro as zk
zk.Compute(uri="zakuro://127.0.0.1:3960")   # raises ConnectionError if /tcp fails
```

## Troubleshooting

### `zakuro-worker: command not found`

The venv isn't active, or the package isn't installed. Verify with `which zakuro-worker` and reinstall with `uv sync --all-extras`.

### Worker exits immediately with a non-zero code

Usually a port collision. `lsof -i :3960` shows the holder; pick a free port with `--port` or let `zk.Worker.spawn()` pick one automatically.

### `/execute` requests hang

The worker runs each call in a thread pool sized to `multiprocessing.cpu_count()`. If all threads are busy with long-running functions, new requests queue indefinitely. Increase parallelism by running more worker processes, or restructure the dispatched functions to be less blocking.

### `503 Worker not ready`

Startup hasn't finished. The thread pool initializer in `on_event("startup")` runs before the app accepts requests, and uvicorn may accept the TCP connection a beat before FastAPI has finished booting. `zk.Worker.spawn()` masks this by polling `/health` with retries; hand-rolled clients should do the same.

### Functions can't import `zakuro` on the worker side

The worker executes cloudpickled functions in its own Python process. If your function imports modules that aren't installed on the worker machine, the call will fail with `ImportError` propagated back to the caller. Either ship a matching environment (same venv, same Docker image) or restructure the function to be self-contained.

### Memory enforcement in standalone mode

When `Compute(uri=None)` falls back to in-process execution, setting `memory=...` raises `RuntimeError`. Memory can only be enforced by a real worker process; remove the constraint or start a worker with `zakuro-worker` / `zk.Worker.spawn()`.
