"""Addressing Zakuro **nodes**.

A worker is not addressable, and this is deliberate. Every node runs its worker
bound to loopback (``http://127.0.0.1:3960``), so the only process that can
reach it is the broker on the same machine. Peer discovery rewrites that
loopback address to ``<peer-ip>:3960`` when it shares the worker around, but
that rewrite is a *guess*: on a ten-node mesh measured 2026-09-20, three nodes
had nothing listening there, and every request routed to them came back as a
proxy error page dressed up as a result.

Asking a broker to place work on a named node does not help either. A
``target_node`` requirement is refused outright for a peer (``503 No workers
available``) and — worse — silently ignored for your own node: a pin to
``node-dd715b7f…`` was measured running on ``zc-broker-2``.

What *is* addressable is a node. Every node runs its own broker on port 9000,
reachable across the mesh, and that broker can always reach its own worker on
loopback. So :func:`nodes` enumerates them and ``zc://node-<fingerprint>``
addresses one, which :class:`~zakuro.compute.Compute` resolves to that node's
broker before it connects.

One caveat worth knowing rather than discovering: **your own broker is the
aggregator**. It knows every worker on the mesh, so addressing it places work
wherever its router likes rather than pinning it locally. A peer's broker knows
only its own worker, so for a peer the two are the same thing. Compare the
``X-Zakuro-Worker`` header against :attr:`NodeInfo.workers` when it matters.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "NodeInfo",
    "nodes",
    "resolve_node",
    "api_key",
    "mesh_proxy_url",
    "reset_cache",
]

#: Ports a locally running broker may have taken. The agent picks one at
#: startup and does not always get 9000 -- on one machine another service held
#: it and the broker landed on 9002 -- so this is scanned, never assumed.
_LOCAL_BROKER_PORTS = (9000, 9002, 9001, 9003, 9004)

#: Where the mesh sidecar publishes its CONNECT proxy. A host that is not
#: itself on the mesh reaches the mesh subnet only through it.
_DEFAULT_MESH_PROXY = "127.0.0.1:18888"

_MESH_PREFIX = "10.13.13."

#: How long a discovery result is reused. Resolving `zc://node-<fp>` re-runs
#: discovery, so a loop over `nodes()` used to rescan the whole mesh once per
#: call -- ten probes per task, and worse than slow: one transient probe
#: timeout made a node vanish mid-loop and `resolve_node` raised LookupError
#: for a node the caller had just been handed. The cache makes a loop see one
#: consistent view. Short enough that a node appearing or going away is picked
#: up within a job's lifetime.
_CACHE_TTL_SECONDS = 30.0

_cache: tuple[float, list[NodeInfo]] | None = None


_account_id: str | None = None


def _home() -> Path:
    return Path(os.environ.get("ZAKURO_HOME", Path.home() / ".zakuro"))


def api_key() -> str | None:
    """The account key, from the environment or from ``zc login``.

    ``ZAKURO_API_KEY`` wins, then the credentials file ``zc login`` writes.
    Without this the broker sees an anonymous caller, which on a billing-enabled
    fleet means every request to a priced worker is refused or stalls -- so
    reading the file is what makes ``import zakuro`` work in a plain shell.
    """
    for var in ("ZAKURO_API_KEY", "ZAKURO_AUTH"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    try:
        for line in (_home() / "credentials").read_text().splitlines():
            m = re.match(r'\s*api_key\s*[:=]\s*"?([^"\s]+)', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def api_url() -> str | None:
    """The dashboard's base URL — the only authority on what an account holds.

    `ZAKURO_API_URL` wins, then the `api_url` line `zc login` writes into the
    credentials file, next to the key.
    """
    value = os.environ.get("ZAKURO_API_URL")
    if value and value.strip():
        return value.strip().rstrip("/")
    try:
        for line in (_home() / "credentials").read_text().splitlines():
            m = re.match(r'\s*api_url\s*[:=]\s*"?([^"\s]+)', line)
            if m:
                return m.group(1).rstrip("/")
    except OSError:
        pass
    return None


def account_id(timeout: float = 6.0) -> str | None:
    """This account's id, as a broker resolves it from the key.

    Asked once per process and cached: the dashboard's balance route is keyed
    by user id, and paying a round trip for it on every reconciliation would
    be the sort of chattiness the batching exists to avoid.
    """
    global _account_id
    if _account_id is not None:
        return _account_id
    key = api_key()
    if not key:
        return None
    import httpx

    for node in nodes():
        host = node.endpoint.split(":")[0]
        try:
            with httpx.Client(proxy=proxy_for(host), timeout=timeout) as client:
                reply = client.get(
                    f"http://{node.endpoint}/me",
                    headers={"Authorization": f"Bearer {key}"},
                )
            if reply.status_code == 200:
                found = reply.json().get("user_id")
                if found:
                    _account_id = str(found)
                    return _account_id
        except Exception:
            continue
    return None


def mesh_proxy_url() -> str | None:
    """The sidecar's CONNECT proxy, or ``None`` when there is none to use.

    Returned only when something is actually listening: handing httpx a proxy
    that is not there turns every mesh call into a connect timeout, which reads
    like a dead mesh rather than a missing sidecar.
    """
    addr = os.environ.get("ZC_MESH_PROXY", _DEFAULT_MESH_PROXY)
    host, _, port = addr.partition(":")
    try:
        with socket.create_connection((host, int(port or 18888)), timeout=1.0):
            return f"http://{addr}"
    except (OSError, ValueError):
        return None


def reset_cache() -> None:
    """Forget the discovery cache, so the next `nodes()` rescans.

    Needed by anything that changes what discovery would find -- a test
    pointing `ZAKURO_HOME` at a different roster, or a caller that has just
    brought a node up and does not want to wait out the TTL.
    """
    global _cache, _account_id
    _cache = None
    _account_id = None


def _needs_proxy(host: str) -> bool:
    """Whether reaching `host` requires the mesh sidecar.

    Loopback never does, and routing it through the proxy would mean leaving
    the machine and coming back -- slower, and broken when the sidecar is down,
    for the one node that is reachable without a mesh at all.
    """
    return host.startswith(_MESH_PREFIX)


def proxy_for(host: str) -> str | None:
    """The proxy to use when talking to `host`, if any."""
    return mesh_proxy_url() if _needs_proxy(host) else None


@dataclass
class NodeInfo:
    """One addressable node, as its own broker describes it."""

    #: ``ip:port`` of the node's broker -- what you actually connect to.
    endpoint: str
    #: ``zc://node-<fingerprint>``; the stable name for the node.
    node_id: str = "?"
    #: Display name the node reports for itself. Not an identity.
    name: str = "?"
    #: zc release the node runs. ``"<pre-#305>"`` when it discloses no build
    #: at all, which dates it rather than leaving it unknown.
    zc_version: str = "unknown"
    #: Account that owns the node, when it says.
    owner_user_id: str | None = None
    #: Names of the workers this node owns -- what to check a placement
    #: against, since addressing your own aggregating broker does not pin.
    workers: set[str] = field(default_factory=set)

    @property
    def fingerprint(self) -> str:
        return self.node_id.replace("zc://node-", "")

    @property
    def uri(self) -> str:
        """The URI that addresses this node: ``zc://node-<fingerprint>``."""
        return self.node_id if self.node_id.startswith("zc://") else f"zc://{self.name}"

    def compute(self, **kwargs: Any) -> Any:
        """A :class:`~zakuro.compute.Compute` addressing this node.

        Preferred over ``Compute(uri=node.uri)`` in a loop: it carries the
        endpoint already, so it does no discovery, cannot be tripped by a
        transient probe timeout, and does not rescan the mesh per task.

        >>> for n in nodes():                       # doctest: +SKIP
        ...     square.to(n.compute())(7)
        """
        from zakuro.compute import Compute

        return Compute(uri=self.uri, _node=self, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name} ({self.fingerprint[:12]}, zc {self.zc_version})"


def _get_json(url: str, timeout: float, headers: dict | None = None) -> Any:
    import httpx

    host = url.split("//", 1)[-1].split(":")[0]
    try:
        with httpx.Client(proxy=proxy_for(host), timeout=timeout) as c:
            r = c.get(url, headers=headers or {})
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


def _endpoints() -> list[str]:
    """Broker endpoints worth probing: the local one first, then the roster.

    The roster is the cache zc keeps, so this keeps working signed out and
    offline -- the same trade ``zc brokers`` makes.
    """
    found: list[str] = []
    for port in _LOCAL_BROKER_PORTS:
        ep = f"127.0.0.1:{port}"
        health = _get_json(f"http://{ep}/health", 1.5)
        if health and health.get("service") == "zakuro-broker":
            found.append(ep)
            break
    try:
        roster = json.loads((_home() / "roster.json").read_text())
        found += [ep for _pk, ep in (roster.get("endpoints") or []) if ep]
    except (OSError, ValueError):
        pass
    return found


def _workers_by_node(endpoint: str) -> dict[str, set[str]]:
    data = _get_json(f"http://{endpoint}/workers", 6.0) or {}
    out: dict[str, set[str]] = {}
    for w in data.get("workers") or []:
        out.setdefault(w.get("node") or "?", set()).add(w.get("name"))
    return out


def nodes(timeout: float = 4.0, refresh: bool = False) -> list[NodeInfo]:
    """Every node whose broker answers, newest information first-hand.

    >>> import zakuro as zk
    >>> for n in zk.nodes():                      # doctest: +SKIP
    ...     print(n)
    zc-worker-1 (e18d8c0ed6ec, zc 0.0.34)

    Each is addressable as ``zk.Compute(uri=n.uri)``.
    """

    def probe(ep: str) -> NodeInfo | None:
        health = _get_json(f"http://{ep}/health", timeout)
        if not health or health.get("service") != "zakuro-broker":
            return None
        build = health.get("build") or {}
        return NodeInfo(
            endpoint=ep,
            node_id=health.get("node_id") or "?",
            name=health.get("node_name") or "?",
            # No build block at all predates the release that added one, so
            # its absence IS the version answer rather than a missing field.
            zc_version=build.get("version") or "<pre-#305>",
            owner_user_id=health.get("owner_user_id"),
        )

    global _cache
    if not refresh and _cache is not None:
        cached_at, cached = _cache
        if time.monotonic() - cached_at < _CACHE_TTL_SECONDS:
            return cached

    endpoints = _endpoints()
    if not endpoints:
        return []
    with ThreadPoolExecutor(max_workers=16) as pool:
        probed = [n for n in pool.map(probe, endpoints) if n is not None]

    # One entry per node: our own appears twice, once on loopback and once at
    # its mesh address, and loopback is the cheaper hop.
    best: dict[str, NodeInfo] = {}
    for n in probed:
        seen = best.get(n.node_id)
        if seen is None or n.endpoint.startswith("127.0.0.1"):
            best[n.node_id] = n

    # One registry read names every node's own workers, for checking that a
    # placement landed where it was addressed.
    if probed:
        owned = _workers_by_node(probed[0].endpoint)
        for n in best.values():
            n.workers = owned.get(n.node_id, set())
    found = sorted(best.values(), key=lambda n: n.name)
    _cache = (time.monotonic(), found)
    return found


_local_broker: str | None = None


def local_broker(timeout: float = 0.3) -> str | None:
    """This machine's broker, ``127.0.0.1:<port>``, or None.

    The one broker an unpinned Task talks to: zc places the work from there
    (``X-Zakuro-Placement: mesh``), so the SDK does not enumerate the mesh.
    Found once per process and remembered; loopback answers in
    milliseconds, so a short timeout per port is plenty.
    """
    global _local_broker
    if _local_broker is not None:
        return _local_broker
    for port in range(9000, 9011):
        health = _get_json(f"http://127.0.0.1:{port}/health", timeout)
        if health and health.get("service") == "zakuro-broker":
            _local_broker = f"127.0.0.1:{port}"
            return _local_broker
    return None


def resolve_node(fingerprint: str, timeout: float = 4.0) -> NodeInfo:
    """The node a ``zc://node-<fingerprint>`` URI names.

    Raises ``LookupError`` naming what *is* reachable, because the usual cause
    is a stale fingerprint copied from an older listing and the useful reply is
    the current one.
    """
    wanted = fingerprint.replace("zc://", "").replace("node-", "").strip()
    found = nodes(timeout=timeout)
    for n in found:
        if n.fingerprint == wanted:
            return n
    # Not in the cached view: rescan once before declaring it unknown. A node
    # that joined a moment ago is a different thing from one that does not
    # exist, and only the second is worth an exception.
    found = nodes(timeout=timeout, refresh=True)
    for n in found:
        if n.fingerprint == wanted:
            return n
    known = ", ".join(f"{n.name}={n.fingerprint[:12]}" for n in found) or "none"
    raise LookupError(
        f"No mesh node with fingerprint {wanted!r}. Reachable now: {known}. "
        "List them with zakuro.nodes()."
    )
