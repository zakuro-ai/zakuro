"""Thin Python wrapper around the ``zakuro-worker`` CLI.

``Worker.spawn()`` runs the CLI binary as a subprocess, polls ``/health``
until it reports ready, and returns a handle with the URI, compute factory,
and lifecycle controls. This keeps notebooks and tests one-liners without
duplicating the subprocess bookkeeping each time.
"""

from __future__ import annotations

import atexit
import contextlib
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from zakuro.compute import Compute


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _locate_zakuro_worker() -> str | None:
    """Find the ``zakuro-worker`` binary that matches the current Python.

    Prefer the bin directory sibling of ``sys.executable`` — this avoids a
    common footgun where a stale ``uv tool install zakuro-ai`` earlier in
    ``$PATH`` resolves to an older wheel whose import chain eagerly pulls
    FastAPI and fails at import time in a reduced environment.

    Falls back to ``shutil.which`` for users who run outside a venv.
    """
    import os as _os
    import sys as _sys

    sibling = Path(_sys.executable).parent / "zakuro-worker"
    if sibling.is_file() and _os.access(sibling, _os.X_OK):
        return str(sibling)
    return shutil.which("zakuro-worker")


class Worker:
    """Background ``zakuro-worker`` process.

    Example:
        >>> worker = Worker.spawn(name="worker-B")
        >>> zk.fn(foo).to(worker.compute())()
        >>> worker.stop()

        with Worker.spawn(name="worker-B") as worker:
            ...  # stops automatically on exit
    """

    _registry: list[Worker] = []

    def __init__(
        self,
        proc: subprocess.Popen,
        host: str,
        port: int,
        name: str,
        transport: str,
    ) -> None:
        self._proc = proc
        self._host = host
        self._port = port
        self._name = name
        self._transport = transport

    @classmethod
    def spawn(
        cls,
        name: str | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
        timeout: float = 15.0,
        transport: str = "http",
    ) -> Worker:
        """Launch the ``zakuro-worker`` CLI and wait for it to become healthy.

        Args:
            name: Optional ``--worker-name`` value (sets ``ZAKURO_WORKER_NAME``).
            host: Bind address. Defaults to loopback.
            port: Bind port. If ``None``, an ephemeral port is chosen.
            timeout: Seconds to wait for ``/health`` to respond.
            transport: ``"http"`` (FastAPI, default) or ``"quic"`` (aioquic).

        Returns:
            Worker handle. Call ``.stop()`` or use as a context manager.
        """
        if transport not in ("http", "quic"):
            raise ValueError(f"unknown transport {transport!r}")
        binary = _locate_zakuro_worker()
        if binary is None:
            raise RuntimeError(
                "`zakuro-worker` CLI not found next to the current Python or on "
                "PATH; install the zakuro package or activate its virtualenv."
            )
        chosen_port = port if port is not None else _free_port()
        args = [
            binary,
            "--host",
            host,
            "--port",
            str(chosen_port),
            "--transport",
            transport,
        ]
        if name:
            args += ["--worker-name", name]

        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        worker = cls(
            proc,
            host,
            chosen_port,
            name or f"worker-{chosen_port}",
            transport,
        )
        cls._registry.append(worker)
        try:
            worker._wait_ready(timeout)
        except Exception:
            worker.stop()
            raise
        return worker

    def _wait_ready(self, timeout: float) -> None:
        if self._transport == "http":
            self._wait_ready_http(timeout)
        else:
            self._wait_ready_quic(timeout)

    def _wait_ready_http(self, timeout: float) -> None:
        deadline = time.time() + timeout
        url = f"http://{self._host}:{self._port}/health"
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"worker {self._name!r} exited early (rc={self._proc.returncode})"
                )
            try:
                r = httpx.get(url, timeout=0.5)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"worker {self._name!r} did not become healthy within {timeout}s")

    def _wait_ready_quic(self, timeout: float) -> None:
        # QUIC has no cheap out-of-band probe; attempt a HEALTH round-trip.
        from zakuro.compute import Compute
        from zakuro.processors.base import ProcessorConfig
        from zakuro.processors.quic import QuicProcessor

        deadline = time.time() + timeout
        last_exc: Exception | None = None
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"worker {self._name!r} exited early (rc={self._proc.returncode})"
                )
            try:
                config = ProcessorConfig(scheme="quic", host=self._host, port=self._port)
                compute = Compute(uri=f"quic://{self._host}:{self._port}", verify=False)
                processor = QuicProcessor(config, compute)
                processor.connect()
                try:
                    if processor.ping():
                        return
                finally:
                    processor.disconnect()
            except Exception as exc:
                last_exc = exc
            time.sleep(0.3)
        raise TimeoutError(
            f"QUIC worker {self._name!r} did not answer HEALTH within "
            f"{timeout}s (last: {last_exc!r})"
        )

    @property
    def uri(self) -> str:
        scheme = "quic" if self._transport == "quic" else "zakuro"
        return f"{scheme}://{self._host}:{self._port}"

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def name(self) -> str:
        return self._name

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def is_running(self) -> bool:
        return self._proc.poll() is None

    def compute(self, **kwargs: Any) -> Compute:
        """Build a ``Compute`` pointing at this worker."""
        from zakuro.compute import Compute

        return Compute(uri=self.uri, **kwargs)

    def info(self) -> dict[str, Any]:
        """Fetch the worker's info payload (transport-appropriate)."""
        if self._transport == "http":
            r = httpx.get(f"http://{self._host}:{self._port}/info", timeout=2.0)
            r.raise_for_status()
            return r.json()
        from zakuro.compute import Compute
        from zakuro.processors.base import ProcessorConfig
        from zakuro.processors.quic import QuicProcessor

        config = ProcessorConfig(scheme="quic", host=self._host, port=self._port)
        compute = Compute(uri=self.uri, verify=False)
        processor = QuicProcessor(config, compute)
        processor.connect()
        try:
            return processor.info()
        finally:
            processor.disconnect()

    def stop(self, timeout: float = 3.0) -> None:
        """Terminate the subprocess if still running."""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self in self._registry:
            self._registry.remove(self)

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        status = "running" if self.is_running else "stopped"
        return f"Worker(name={self._name!r}, uri={self.uri!r}, status={status})"


@atexit.register
def _cleanup_workers() -> None:
    for worker in list(Worker._registry):
        with contextlib.suppress(Exception):
            worker.stop()
