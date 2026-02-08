"""Worker discovery via Tailscale and DNS."""

from __future__ import annotations

import json
import socket
import subprocess
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from zakuro.config import Config


def discover_worker(config: Optional[Config] = None) -> str:
    """
    Discover available worker.

    Strategy:
    1. Try Tailscale peers with 'zakuro-worker' hostname
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

    # Try Tailscale
    if config.tailscale_enabled:
        worker = _discover_tailscale()
        if worker:
            return worker

    # Try DNS
    worker = _discover_dns()
    if worker:
        return worker

    # Fallback
    return config.default_host


def _discover_tailscale() -> Optional[str]:
    """Discover worker via Tailscale status."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        status = json.loads(result.stdout)

        # Find peer with 'zakuro-worker' in hostname
        peers = status.get("Peer", {})
        for peer in peers.values():
            hostname = peer.get("HostName", "")
            if "zakuro-worker" in hostname.lower():
                ips = peer.get("TailscaleIPs", [])
                if ips:
                    return str(ips[0])

        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def _discover_dns() -> Optional[str]:
    """Discover worker via DNS."""
    hostnames = [
        "zakuro-worker",
        "zakuro-worker.local",
        "zakuro-worker.tailscale",
    ]

    for hostname in hostnames:
        try:
            ip = socket.gethostbyname(hostname)
            return ip
        except socket.gaierror:
            continue

    return None


def list_workers(config: Optional[Config] = None) -> list[str]:
    """
    List all available workers.

    Returns:
        List of worker hostnames/IPs
    """
    workers: list[str] = []

    if config is None:
        from zakuro.config import Config

        config = Config.load()

    # Try Tailscale
    if config.tailscale_enabled:
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                status = json.loads(result.stdout)
                for peer in status.get("Peer", {}).values():
                    hostname = peer.get("HostName", "")
                    if "zakuro" in hostname.lower():
                        ips = peer.get("TailscaleIPs", [])
                        if ips:
                            workers.append(str(ips[0]))
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass

    return workers
