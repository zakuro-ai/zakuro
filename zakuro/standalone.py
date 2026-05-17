"""Backend detection with in-process (standalone) fallback.

When a user creates a Compute without specifying a URI or host, Zakuro tries to
find a reachable backend. If none is found (no local worker, no broker, no zc
CLI), the function runs in-process.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import socket
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zakuro.compute import Compute
    from zakuro.config import Config

_PROBE_TIMEOUT_SECONDS = 0.5
_LOCAL_WORKER_HOST = "127.0.0.1"
_LOCAL_WORKER_PORT = 3960
_LOCAL_BROKER_PORT = 9000


def detect_backend(config: Config | None = None) -> str | None:
    """Detect a reachable Zakuro backend.

    Probe order:
        1. Local worker at 127.0.0.1:3960
        2. Local broker at 127.0.0.1:9000
        3. Configured broker (e.g. my.zakuro-ai.com:9000) — only if zc CLI is
           installed, since the remote broker requires zc for auth/routing
        4. Tailscale peer tagged zakuro-worker

    Returns:
        A URI string (``zakuro://...`` or ``zc://...``) pointing at the
        reachable backend, or ``None`` if nothing is reachable. ``None`` means
        the caller should execute in-process (standalone mode).
    """
    if os.environ.get("ZAKURO_STANDALONE", "").lower() == "force":
        return None

    if config is None:
        from zakuro.config import Config

        config = Config.load()

    if _tcp_reachable(_LOCAL_WORKER_HOST, _LOCAL_WORKER_PORT):
        return f"zakuro://{_LOCAL_WORKER_HOST}:{_LOCAL_WORKER_PORT}"

    if _tcp_reachable(_LOCAL_WORKER_HOST, _LOCAL_BROKER_PORT):
        return f"zc://{_LOCAL_WORKER_HOST}:{_LOCAL_BROKER_PORT}"

    if _has_zc_cli() and _tcp_reachable(config.default_host, config.default_port):
        return f"zc://{config.default_host}:{config.default_port}"

    if config.tailscale_enabled:
        peer = _discover_tailscale_worker()
        if peer:
            return f"zakuro://{peer}:{_LOCAL_WORKER_PORT}"

    return None


def is_standalone() -> bool:
    """Return True if no backend is reachable and the fallback will run locally."""
    return detect_backend() is None


def _has_zc_cli() -> bool:
    """Check whether the ``zc`` CLI is on PATH."""
    return shutil.which("zc") is not None


def _tcp_reachable(host: str, port: int) -> bool:
    """Return True if a TCP connection to host:port succeeds within the probe timeout."""
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except (OSError, socket.gaierror):
        return False


def _discover_tailscale_worker() -> str | None:
    """Re-use the existing Tailscale discovery and verify reachability."""
    from zakuro.discovery import _discover_tailscale

    ip = _discover_tailscale()
    if ip and _tcp_reachable(ip, _LOCAL_WORKER_PORT):
        return ip
    return None


# Env vars set from Compute.cpus/gpus during standalone execution. Respected by
# numpy, PyTorch, scipy, BLAS backends, and CUDA — so the hints actually shape
# runtime parallelism for the common numerical libraries.
_CPU_ENV_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def ensure_standalone_is_safe(compute: Compute) -> None:
    """Reject standalone execution when the requested resources cannot be honored.

    Standalone mode can only apply advisory CPU/GPU hints via environment
    variables. Memory has no reliable in-process enforcement path (``RLIMIT_AS``
    is process-wide and not portable across macOS/Linux), so any explicit
    memory constraint is refused rather than silently ignored.
    """
    if compute.memory is not None:
        raise RuntimeError(
            f"Memory limit {compute.memory!r} cannot be enforced in standalone "
            "(in-process) mode. Start a local worker (`zc worker start`), pass "
            "a reachable backend via Compute(uri=...), or drop the memory "
            "constraint by constructing Compute without `memory=...`."
        )


@contextlib.contextmanager
def applied_resource_limits(compute: Compute) -> Iterator[None]:
    """Apply advisory CPU/GPU hints from ``compute`` for the duration of a call.

    Standalone mode has no real kernel-level isolation — the user opted in to
    in-process execution when detection failed. What we *can* do is set the
    environment variables that numerical libraries read when they pick thread
    pool sizes, and zero out CUDA visibility when ``gpus=0``. Memory is not
    enforced here (would require a subprocess with ``RLIMIT_AS``).
    """
    cpu_threads = max(1, math.ceil(compute.cpus))
    overrides: dict[str, str] = {var: str(cpu_threads) for var in _CPU_ENV_VARS}
    if compute.gpus == 0:
        overrides["CUDA_VISIBLE_DEVICES"] = ""

    previous: dict[str, str | None] = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
