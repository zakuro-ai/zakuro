"""FastAPI worker server for executing remote functions."""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cloudpickle
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from zakuro.worker.executor import execute_function

app = FastAPI(
    title="Zakuro Worker",
    description="Worker node for Zakuro distributed computing",
    version="0.2.0",
)

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


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


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
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
            gpus = len(lines)
            if lines:
                parts = lines[0].split(", ", 1)
                gpu_model = parts[0].strip()
                if len(parts) > 1:
                    try:
                        gpu_vram_gb = int(parts[1].strip()) // 1024
                    except ValueError:
                        pass
    except Exception:
        pass

    # CPU model
    cpu_model = platform.processor() or platform.machine() or None

    # Available storage on root filesystem
    storage_gb_available = None
    try:
        storage_gb_available = shutil.disk_usage("/").free // (1024 ** 3)
    except Exception:
        pass

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
            "cpu_price": float(os.environ.get("ZAKURO_CPU_PRICE", "0.001")),
            "memory_price": float(os.environ.get("ZAKURO_MEMORY_PRICE", "0.0001")),
            "gpu_price": float(os.environ.get("ZAKURO_GPU_PRICE", "0.01")),
            "min_charge": float(os.environ.get("ZAKURO_MIN_CHARGE", "0.0001")),
        },
        "tags": os.environ.get("ZAKURO_WORKER_TAGS", "").split(",") if os.environ.get("ZAKURO_WORKER_TAGS") else [],
    }


@app.post("/execute")
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

    payload = await request.body()

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
