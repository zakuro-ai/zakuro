"""FastAPI worker server for executing remote functions."""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cloudpickle

try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse
except ImportError as exc:
    raise ImportError(
        "Running the Zakuro worker requires the `[worker]` extra "
        "(fastapi, uvicorn, psutil). Install it with "
        "`pip install 'zakuro-ai[worker]'` (or `uv pip install '.[worker]'` "
        "from a source checkout)."
    ) from exc

import contextlib

from zakuro.observability import (
    init_logging,
    init_metrics,
    init_sentry,
    mount_metrics_endpoint,
)
from zakuro.wire import WireError
from zakuro.worker.envelope import unwrap_payload
from zakuro.worker.executor import execute_function

# Init structured logging + Sentry + Prometheus as early as possible so any
# exception during app/executor wiring is captured + emitted as JSON and
# metrics are immediately scrapeable. All three are no-ops when their
# optional dep is missing or the relevant env var is unset.
init_logging("worker")
init_sentry("worker")
init_metrics()

app = FastAPI(
    title="Zakuro Worker",
    description="Worker node for Zakuro distributed computing",
    version="0.2.0",
)

# /metrics endpoint, no-op when prometheus_client is not installed. Once
# RFC 0002 lands, gate this on the metrics:read JWT scope.
mount_metrics_endpoint(app)

# Thread pool for function execution (threads share memory, so instance
# state in executor._instances is visible to all workers)
executor: ThreadPoolExecutor | None = None


@app.on_event("startup")
async def startup() -> None:
    """Initialize thread pool on startup."""
    global executor
    executor = ThreadPoolExecutor(
        max_workers=multiprocessing.cpu_count(),
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    """Cleanup on shutdown."""
    global executor
    if executor:
        executor.shutdown(wait=True)


async def _check_event_loop_responsive() -> bool:
    """Yield to the loop and confirm we get scheduled back. Catches a
    hung loop scenario where the worker process is up but starved."""
    try:
        await asyncio.wait_for(asyncio.sleep(0), timeout=1.0)
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return False


async def _check_executor_ready() -> bool:
    """Has the FastAPI startup event fired and the executor been wired?"""
    return executor is not None


async def _check_broker_reachable() -> tuple[bool, str | None]:
    """Probe ZAKURO_BROKER_URL/health with a 2s timeout.

    Returns (True, None) if the env var is unset (worker is broker-less),
    (True, None) on a 2xx response, (False, reason) otherwise.
    """
    import os

    broker_url = os.environ.get("ZAKURO_BROKER_URL", "").strip()
    if not broker_url:
        return True, None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{broker_url.rstrip('/')}/health")
        if 200 <= resp.status_code < 300:
            return True, None
        return False, f"broker {broker_url} returned {resp.status_code}"
    except Exception as exc:
        return False, f"broker {broker_url} unreachable: {exc.__class__.__name__}"


async def _check_storage_reachable() -> tuple[bool, str | None]:
    """Probe the configured MinIO endpoint with a 2s timeout.

    Returns (True, None) if no storage is configured (the worker doesn't
    depend on a bucket). Otherwise calls `list_buckets()` with a short
    timeout and reports the outcome.
    """
    import os

    endpoint = os.environ.get("ZAKURO_S3_ENDPOINT", "").strip()
    access_key = os.environ.get("ZAKURO_S3_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("ZAKURO_S3_SECRET_KEY", "").strip()
    if not endpoint or not access_key or not secret_key:
        return True, None
    try:
        from minio import Minio  # type: ignore[import-not-found]

        client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=os.environ.get("ZAKURO_S3_SECURE", "true").lower() == "true",
        )
        # list_buckets is a cheap auth + reachability probe.
        await asyncio.wait_for(asyncio.to_thread(client.list_buckets), timeout=2.0)
        return True, None
    except asyncio.TimeoutError:
        return False, f"storage {endpoint} timed out (2s)"
    except Exception as exc:
        return False, f"storage {endpoint} unreachable: {exc.__class__.__name__}"


@app.get("/live")
async def live() -> dict[str, str]:
    """Liveness probe: process up + event loop responsive.

    Cheap, dependency-free. Kubernetes uses this to decide whether to
    SIGKILL the pod. Never fail this unless the process is genuinely
    unrecoverable — failing it triggers a pod restart.
    """
    if not await _check_event_loop_responsive():
        raise HTTPException(status_code=503, detail="event loop unresponsive")
    return {"status": "alive"}


@app.get("/ready")
async def ready() -> Response:
    """Readiness probe: deep checks for the dependencies we need to serve.

    Returns 200 only when:
      - the FastAPI startup event has wired the thread-pool executor,
      - the broker (if configured) responds 2xx within 2s,
      - the object store (if configured) lists buckets within 2s.

    A 503 here tells Kubernetes to drop the pod out of the Service while
    leaving the process alive so in-flight requests can drain. (#126)
    """
    failures: list[str] = []
    checks: dict[str, str | bool] = {}

    if not await _check_executor_ready():
        failures.append("executor not initialised")
        checks["executor"] = "not-ready"
    else:
        checks["executor"] = "ready"

    broker_ok, broker_reason = await _check_broker_reachable()
    checks["broker"] = "ready" if broker_ok else (broker_reason or "fail")
    if not broker_ok:
        failures.append(broker_reason or "broker check failed")

    storage_ok, storage_reason = await _check_storage_reachable()
    checks["storage"] = "ready" if storage_ok else (storage_reason or "fail")
    if not storage_ok:
        failures.append(storage_reason or "storage check failed")

    status_code = 200 if not failures else 503
    body = {
        "status": "ready" if not failures else "not-ready",
        "checks": checks,
        "failures": failures,
    }
    return JSONResponse(status_code=status_code, content=body)


@app.get("/health")
async def health() -> Response:
    """Legacy alias for `/ready`.

    Kept for backwards compatibility with existing Docker HEALTHCHECK
    consumers and any external monitors that hit `/health`. New
    integrations should use `/live` and `/ready` directly.
    """
    return await ready()


@app.get("/info")
async def info() -> dict[str, Any]:
    """Worker info endpoint for broker discovery."""
    import os
    import platform
    import shutil
    import subprocess

    cpus = multiprocessing.cpu_count()

    # Memory info
    try:
        import psutil

        vm = psutil.virtual_memory()
        memory_total = vm.total
        memory_available = vm.available
    except ImportError:
        memory_total = 8 * 1024 * 1024 * 1024
        memory_available = memory_total

    # GPU info from nvidia-smi
    gpus = 0
    gpu_model = None
    gpu_vram_gb = None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = [x.strip() for x in result.stdout.strip().split("\n") if x.strip()]
            gpus = len(lines)
            if lines:
                parts = lines[0].split(", ", 1)
                gpu_model = parts[0].strip()
                if len(parts) > 1:
                    with contextlib.suppress(ValueError):
                        gpu_vram_gb = int(parts[1].strip()) // 1024
    except Exception:
        pass

    # CPU model
    cpu_model = platform.processor() or platform.machine() or None

    # Available storage on root filesystem
    storage_gb_available = None
    with contextlib.suppress(Exception):
        storage_gb_available = shutil.disk_usage("/").free // (1024**3)

    # Worker name from environment or hostname
    worker_name = os.environ.get("ZAKURO_WORKER_NAME", f"worker-{platform.node()}")

    return {
        "name": worker_name,
        "worker_type": os.environ.get("ZAKURO_WORKER_TYPE", "zakuro"),
        "version": "0.2.0",
        "resources": {
            "cpus_total": float(cpus),
            "cpus_available": float(cpus),
            "memory_total": memory_total,
            "memory_available": memory_available,
            "gpus_total": gpus,
            "gpus_available": gpus,
        },
        "hardware": {
            "cpu_model": cpu_model,
            "gpu_model": gpu_model,
            "gpu_vram_gb": gpu_vram_gb,
            "storage_gb": storage_gb_available,
        },
        "pricing": {
            "price_per_hour": float(os.environ.get("ZAKURO_PRICE_PER_HOUR", "3.6")),
            "min_charge": float(os.environ.get("ZAKURO_MIN_CHARGE", "0.001")),
        },
        "tags": os.environ.get("ZAKURO_WORKER_TAGS", "").split(",")
        if os.environ.get("ZAKURO_WORKER_TAGS")
        else [],
    }


@app.post("/execute")
async def execute(request: Request) -> Response:
    """
    Execute a serialized function.

    Wire format depends on ``ZAKURO_WIRE``:

    * ``v1`` — body is a postcard-encoded ``zakuro_wire::Envelope``.
      The worker decodes + HMAC-verifies it via
      :func:`zakuro.worker.envelope.unwrap_payload`; the envelope's
      ``callable`` blob (cloudpickle bytes) is then passed to
      ``execute_function``. HMAC / decode failures return 401 with
      empty body.
    * unset — body is the raw cloudpickle dict (legacy path). Behaviour
      is identical to pre-#117 builds. This branch will be removed once
      the v0.4 rollout completes.
    """
    global executor
    if executor is None:
        raise HTTPException(status_code=503, detail="Worker not ready")

    raw = await request.body()

    try:
        inner_payload, _envelope = unwrap_payload(raw)
    except WireError:
        # Empty body — leaking the reason would aid enumeration. The
        # specific reason class is recorded in metrics by `unwrap_payload`
        # for operator triage (forthcoming when #185 metrics + #189 auth
        # land; the metric increments degrade gracefully today).
        return Response(content=b"", status_code=401)

    try:
        # Run in thread pool (shared memory for instance state)
        loop = asyncio.get_event_loop()
        result_bytes = await loop.run_in_executor(
            executor,
            execute_function,
            inner_payload,
        )

        return Response(
            content=result_bytes,
            media_type="application/octet-stream",
        )
    except Exception as e:
        # Serialize exception
        return Response(
            content=cloudpickle.dumps(e),
            media_type="application/octet-stream",
            status_code=200,  # 200 because error is in payload
        )


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "service": "Zakuro Worker",
        "version": "0.2.0",
        "docs": "/docs",
    }


def main() -> None:
    """Run the worker server."""
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description="Zakuro Worker Server")
    parser.add_argument("--host", default=os.environ.get("ZAKURO_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ZAKURO_PORT", "3960")))
    parser.add_argument("--worker-name", default=None, help="Override ZAKURO_WORKER_NAME")
    args = parser.parse_args()

    if args.worker_name:
        os.environ["ZAKURO_WORKER_NAME"] = args.worker_name

    uvicorn.run(
        "zakuro.worker.server:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
