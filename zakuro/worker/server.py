"""FastAPI worker server for executing remote functions."""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
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

# Process pool for function execution
executor: ProcessPoolExecutor | None = None


@app.on_event("startup")
async def startup() -> None:
    """Initialize process pool on startup."""
    global executor
    executor = ProcessPoolExecutor(
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

    cpus = multiprocessing.cpu_count()

    # Try to get memory info
    try:
        import psutil
        memory_total = psutil.virtual_memory().total
        memory_available = psutil.virtual_memory().available
    except ImportError:
        memory_total = 8 * 1024 * 1024 * 1024  # Default 8 GiB
        memory_available = memory_total

    # Check for GPUs
    gpus = 0
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            gpus = len([line for line in result.stdout.split("\n") if line.strip()])
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
        # Run in process pool for isolation
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
    import uvicorn

    uvicorn.run(
        "zakuro.worker.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
