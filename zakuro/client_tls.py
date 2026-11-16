"""Client-side mutual TLS for the SDK's direct connections (``ZAKURO_CLIENT_CERT_DIR``, ADR 0013).

When ``ZAKURO_CLIENT_CERT_DIR`` is set, the direct HTTP client and the QUIC
processor present ``tls.crt``/``tls.key`` as their client certificate and
verify the worker against ``ca.crt``. When it is unset nothing here is used and
connections behave exactly as before. A set-but-incomplete directory raises
:class:`ClientTlsError` -- the SDK never silently falls back to no certificate.
zc-mediated traffic is not affected.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

ENV_VAR = "ZAKURO_CLIENT_CERT_DIR"
_FILES = ("tls.crt", "tls.key", "ca.crt")


class ClientTlsError(RuntimeError):
    """``ZAKURO_CLIENT_CERT_DIR`` is set but unusable."""


def client_cert_dir() -> Path | None:
    raw = os.environ.get(ENV_VAR, "").strip()
    return Path(raw).expanduser() if raw else None


def load_client_tls() -> tuple[Path, Path, Path] | None:
    """Return ``(cert, key, ca)`` paths, ``None`` when unset; raise when incomplete."""
    base = client_cert_dir()
    if base is None:
        return None
    paths = []
    for name in _FILES:
        p = base / name
        if not p.is_file():
            raise ClientTlsError(f"{ENV_VAR}={base}: missing required file {name}")
        paths.append(p)
    return paths[0], paths[1], paths[2]


def client_ssl_context() -> ssl.SSLContext | None:
    """SSL context with the client cert loaded and the worker verified against ``ca.crt``."""
    material = load_client_tls()
    if material is None:
        return None
    cert, key, ca = material
    try:
        ctx = ssl.create_default_context(cafile=str(ca))
        ctx.load_cert_chain(str(cert), str(key))
    except (ssl.SSLError, OSError) as exc:
        raise ClientTlsError(f"{ENV_VAR}: cannot load client TLS material: {exc}") from exc
    return ctx


def apply_quic_client_tls(config):  # type: ignore[no-untyped-def]
    """Configure an aioquic client ``QuicConfiguration`` for mTLS; no-op when unset."""
    material = load_client_tls()
    if material is None:
        return config
    cert, key, ca = material
    try:
        config.load_cert_chain(str(cert), str(key))
        config.load_verify_locations(cafile=str(ca))
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise ClientTlsError(f"{ENV_VAR}: cannot load client TLS material: {exc}") from exc
    config.verify_mode = ssl.CERT_REQUIRED
    return config
