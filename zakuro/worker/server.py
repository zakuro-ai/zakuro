"""FastAPI worker server for executing remote functions."""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cloudpickle

try:
    from fastapi import FastAPI, HTTPException, Request, Response
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
from zakuro.worker.envelope import EnvelopeRejectedError, unwrap_payload
from zakuro.worker.executor import execute_function
from zakuro.worker.posture import max_payload_bytes, validate_auth_config, validate_wire_config

app = FastAPI(
    title="Zakuro Worker",
    description="Worker node for Zakuro distributed computing",
    version=__version__,
)

# Thread pool for function execution (threads share memory, so instance
# state in executor._instances is visible to all workers)
executor: ThreadPoolExecutor | None = None


@app.on_event("startup")
async def startup() -> None:
    """Initialize thread pool on startup."""
    global executor
    validate_auth_config()
    validate_wire_config()
    executor = ThreadPoolExecutor(
        max_workers=multiprocessing.cpu_count(),
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    """Cleanup on shutdown."""
    global executor
    if executor:
        executor.shutdown(wait=True)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/info", dependencies=[Depends(require_jwt_scope("worker:info"))])
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
            "price_per_hour": float(os.environ.get("ZAKURO_PRICE_PER_HOUR", "3.6")),
            "min_charge": float(os.environ.get("ZAKURO_MIN_CHARGE", "0.001")),
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


@app.post("/execute", dependencies=[Depends(require_jwt_scope("exec:fn"))])
async def execute(request: Request) -> Response:
    """
    Execute a serialized function.

    Expects cloudpickle-serialized payload with:
    - func: The function to execute
    - args: Positional arguments
    - kwargs: Keyword arguments

    Returns cloudpickle-serialized result.
    """
    global executor
    if executor is None:
        raise HTTPException(status_code=503, detail="Worker not ready")

    payload = await _read_capped_body(request)
    try:
        payload = unwrap_payload(payload)  # ZAKURO_WIRE=v1: verify HMAC before any unpickling
    except EnvelopeRejectedError:
        return Response(status_code=401)

    try:
        # Run in thread pool (shared memory for instance state)
        loop = asyncio.get_event_loop()
        result_bytes = await loop.run_in_executor(
            executor,
            execute_function,
            payload,
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

    from zakuro.worker.posture import StartupConfigError
    from zakuro.worker.tls import uvicorn_ssl_kwargs

    try:
        ssl_kwargs = uvicorn_ssl_kwargs()
    except StartupConfigError as exc:
        raise SystemExit(str(exc)) from exc

    uvicorn.run(
        "zakuro.worker.server:app",
        host=args.host,
        port=args.port,
        reload=False,
        **ssl_kwargs,
    )


if __name__ == "__main__":  # pragma: no cover - script entry point
    main()
