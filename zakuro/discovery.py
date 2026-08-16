"""Worker discovery via DNS.

The mesh is WireGuard: peers hold ordinary addresses on 10.13.13.0/24 and are
reached by name or IP. There is no peer-listing daemon to interrogate, which is
what this module used to shell out to.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zakuro.config import Config


def discover_worker(config: Config | None = None) -> str:
    """
    Discover available worker.

    Strategy:
    1. Try DNS names
    2. Try DNS resolution of 'zakuro-worker'
    3. Fallback to localhost

    Args:
        config: Optional configuration to use

    Returns:
        Worker hostname or IP address
    """
    if config is None:
        from zakuro.config import Config

        config = Config.load()

    # Try DNS
    worker = _discover_dns()
    if worker:
        return worker

    # Fallback
    return config.default_host


def _discover_dns() -> str | None:
    """Discover worker via DNS."""
    hostnames = [
        "zakuro-worker",
        "zakuro-worker.local",
    ]

    for hostname in hostnames:
        try:
            ip = socket.gethostbyname(hostname)
            return ip
        except socket.gaierror:
            continue

    return None


def list_workers(config: Config | None = None) -> list[str]:
    """
    List all available workers.

    Returns:
        List of worker hostnames/IPs
    """
    workers: list[str] = []

    if config is None:
        from zakuro.config import Config

        config = Config.load()

    return workers
