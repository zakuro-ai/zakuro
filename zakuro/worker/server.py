"""FastAPI worker server for executing remote functions."""

from __future__ import annotations

import asyncio
import multiprocessing
from typing import Any

import cloudpickle
from starlette.types import ASGIApp, Receive, Scope, Send

try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - requires the [worker] extra to be absent
    raise ImportError(
        "Running the Zakuro worker requires the `[worker]` extra "
        "(fastapi, uvicorn, psutil). Install it with "
        "`pip install 'zakuro-ai[worker]'` (or `uv pip install '.[worker]'` "
        "from a source checkout)."
    ) from exc

import contextlib

from fastapi import Depends

from zakuro import __version__
from zakuro.auth.middleware import require_jwt_scope
from zakuro.observability import (
    init_logging,
    init_metrics,
    init_sentry,
    init_tracing,
    instrument_fastapi_app,
    instrument_httpx,
    mount_metrics_endpoint,
)
from zakuro.worker.envelope import EnvelopeRejectedError, unwrap_payload
from zakuro.worker.executor import execute_function
from zakuro.worker.pool import JobPool
from zakuro.worker.posture import max_payload_bytes, validate_auth_config, validate_wire_config

# Scoped dependencies built once at import so FastAPI captures them in its
# dependant tree.
_REQUIRE_EXEC = require_jwt_scope("exec:fn")
_REQUIRE_INFO = require_jwt_scope("worker:info")

# Structured logging + Sentry + Prometheus + OpenTelemetry, as early as
# possible. Each is a no-op when its optional dependency (the
# ``[observability]`` extra) is missing or its env var (SENTRY_DSN /
# OTEL_EXPORTER_OTLP_ENDPOINT) is unset.
init_logging("worker")
init_sentry("worker")
init_metrics()
init_tracing("worker")
instrument_httpx()  # outbound calls carry W3C traceparent for cross-hop stitching

app = FastAPI(
    title="Zakuro Worker",
    description="Worker node for Zakuro distributed computing",
    version=__version__,
)

# /metrics endpoint; no-op when prometheus_client is not installed.
mount_metrics_endpoint(app)
# FastAPI auto-instrumentation; no-op without the extra. Must follow ``app``.
instrument_fastapi_app(app)

# Thread pool for function execution (threads share memory, so instance
# state in executor._instances is visible to all workers)
executor: JobPool | None = None


@app.on_event("startup")
async def startup() -> None:
    """Initialize thread pool on startup."""
    global executor
    validate_auth_config()
    validate_wire_config()
    executor = JobPool(max_workers=multiprocessing.cpu_count())


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
        from minio import Minio

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
    """Legacy alias for ``/ready``.

    Kept for Docker HEALTHCHECK consumers and monitors that hit ``/health``
    (status code is what they read). New integrations should use ``/live``
    and ``/ready`` directly.
    """
    return await ready()


@app.get("/info", dependencies=[Depends(_REQUIRE_INFO)])
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
    except ImportError:  # pragma: no cover - psutil ships with the [worker] extra
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
            lines = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
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
        "version": __version__,
        # The interpreter that will actually run the job (zc#327).
        #
        # A cloudpickled function carries raw CPython bytecode, and opcode
        # numbers are renumbered between minor versions -- LOAD_FAST is 124 up
        # to 3.12 and 85 from 3.13. A worker on a different minor version than
        # the payload was built for therefore answers EVERY request with
        # `SystemError: unknown opcode`, while being completely healthy.
        #
        # A worker record advertises CPUs, RAM and GPUs but never said this, so
        # a broker could not tell that a worker was incapable of running a
        # given payload and routed to it anyway. Measured on a ten-node mesh:
        # four workers on 3.13.9, the rest on 3.12.3, and 13 of 19 requests
        # failing with nothing wrong on either side.
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
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
            "price_per_hour": float(os.environ.get("ZAKURO_PRICE_PER_HOUR", "540")),
            "min_charge": float(os.environ.get("ZAKURO_MIN_CHARGE", "0.15")),
        },
        "tags": os.environ.get("ZAKURO_WORKER_TAGS", "").split(",")
        if os.environ.get("ZAKURO_WORKER_TAGS")
        else [],
    }


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body, raising 413 once it exceeds the payload cap.

    A declared ``Content-Length`` over the cap is rejected before any body is
    read; chunked / undeclared bodies are counted while streaming and aborted
    as soon as the cap is crossed, so memory use stays bounded by the cap.
    """
    limit = max_payload_bytes()
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_big = int(declared) > limit
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
        if too_big:
            raise HTTPException(status_code=413, detail=f"Payload exceeds {limit} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"Payload exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/execute", dependencies=[Depends(_REQUIRE_EXEC)])
async def execute(request: Request) -> Response:
    """
    Execute a serialized function.

    Expects cloudpickle-serialized payload with:
    - func: The function to execute
    - args: Positional arguments
    - kwargs: Keyword arguments

    Returns cloudpickle-serialized result. Served by :class:`_ExecuteFastPath`
    in front of the app; this route answers when the app runs without it.
    """
    return await _execute_response(request)


async def _execute_response(request: Request) -> Response:
    """The work of ``POST /execute``, once the caller is authorized."""
    if executor is None:
        raise HTTPException(status_code=503, detail="Worker not ready")

    payload = await _read_capped_body(request)
    try:
        # ZAKURO_WIRE=v1|v2: verify the HMAC before any unpickling.
        payload, _envelope = unwrap_payload(payload)
    except EnvelopeRejectedError:
        return Response(status_code=401)

    try:
        # Run in thread pool (shared memory for instance state)
        result_bytes = await executor.run(execute_function, payload)

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


class _ExecuteFastPath:
    """Serve ``POST /execute`` past FastAPI's routing, dependency solving and
    middleware stack; every other request goes to the app.

    For a one-endpoint job server the framework cost the worker several times
    the job: a py-spy profile under load put ~60 % of the worker's CPU in the
    FastAPI/Starlette stack and ~16 % in running the function (2026-11-21). The
    caller is authorized by the same dependency as the route, and the payload
    cap and envelope check are the route's own code.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/execute" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            await _REQUIRE_EXEC(request)
            response = await _execute_response(request)
        except HTTPException as exc:
            # What FastAPI's default handler answers for it.
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
        await response(scope, receive, send)


# Inside Starlette's ServerErrorMiddleware, so an unexpected error is still a 500.
app.add_middleware(_ExecuteFastPath)


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "service": "Zakuro Worker",
        "version": __version__,
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

    from zakuro.worker.posture import InsecureBindError, StartupConfigError, resolve_listener
    from zakuro.worker.tls import uvicorn_ssl_kwargs

    try:
        # Fail-closed bind policy: refuse a routable bind with no caller auth.
        host = resolve_listener(args.host, args.port)
        ssl_kwargs = uvicorn_ssl_kwargs()
    except (InsecureBindError, StartupConfigError) as exc:
        raise SystemExit(str(exc)) from exc

    from zakuro.worker.cli import access_log_enabled

    uvicorn.run(
        "zakuro.worker.server:app",
        host=host,
        port=args.port,
        reload=False,
        access_log=access_log_enabled(),
        **ssl_kwargs,
    )


if __name__ == "__main__":  # pragma: no cover - script entry point
    main()
