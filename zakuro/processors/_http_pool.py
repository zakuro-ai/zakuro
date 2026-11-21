"""One HTTP client per thread per broker.

Each SDK call used to build an ``httpx.Client`` and close it afterwards, so
every call opened a new TCP connection to the broker. Clients here are kept
and reused, one per (process, thread, broker URL, headers, proxy):

- per thread, because callers fan calls out over thread pools (``Task``,
  ``AdaptiveCompute``) and a client is cheapest to reason about unshared;
- per process, because a forked child must never write to sockets it
  inherited from its parent.

Idle connections are dropped after 30 s, so a kept connection does not
outlive a broker that restarted for long.
"""

from __future__ import annotations

import atexit
import os
import threading

import httpx

_KEEPALIVE_EXPIRY_S = 30.0

_lock = threading.Lock()
_clients: dict[tuple[object, ...], httpx.Client] = {}


def shared_client(
    base_url: str,
    headers: dict[str, str],
    proxy: str | None,
    timeout: httpx.Timeout,
) -> httpx.Client:
    """The kept client for this process, thread, broker and credentials."""
    key = (
        os.getpid(),
        threading.get_ident(),
        base_url,
        tuple(sorted(headers.items())),
        proxy,
    )
    with _lock:
        client = _clients.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(
                base_url=base_url,
                headers=headers,
                proxy=proxy,
                timeout=timeout,
                limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_EXPIRY_S),
            )
            _clients[key] = client
        return client


def close_all() -> None:
    """Close and forget every kept client (at exit, and between tests)."""
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


atexit.register(close_all)
