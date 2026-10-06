"""Refuse to send the account key over cleartext HTTP (ZAKURO-0017).

The key is a bearer credential: anyone on the path can read and replay it. It
may therefore only be attached to a request whose URL is ``https://``, loopback,
or a mesh address (reached through the sidecar's encrypted tunnel). A plain
``http://`` URL to anything else is refused unless ``ZAKURO_ALLOW_INSECURE_HTTP``
is set to a truthy value.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

ALLOW_INSECURE_ENV = "ZAKURO_ALLOW_INSECURE_HTTP"


class InsecureTransportError(RuntimeError):
    """Raised when the account key would be sent over plain http."""


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def insecure_http_allowed() -> bool:
    return os.environ.get(ALLOW_INSECURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_secure_for_key(url: str) -> None:
    """Raise :class:`InsecureTransportError` if the key must not go to ``url``."""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    if parts.scheme.lower() != "http":
        return
    host = parts.hostname or ""
    if _is_loopback(host) or insecure_http_allowed():
        return
    from zakuro.mesh import _needs_proxy

    if _needs_proxy(host):
        return
    raise InsecureTransportError(
        f"Refusing to send the account key over plain http to {host!r}. "
        f"Use an https:// URL, or set {ALLOW_INSECURE_ENV}=1 to allow it "
        "(self-hosted plain-http hub)."
    )
