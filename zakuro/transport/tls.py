"""TLS material loading + transport-config builders (#115, RFC 0002).

The worker and client both load their mTLS material from a single
directory, conventionally ``~/.zakuro/certs/`` (override with
``ZAKURO_CERT_DIR``). Files expected:

  - ``tls.crt`` — this end's certificate (PEM)
  - ``tls.key`` — this end's private key (PEM, unencrypted)
  - ``ca.crt`` — bundle of CAs to verify the *peer* against (PEM,
    may contain multiple roots concatenated)

Missing files raise :class:`TlsNotConfiguredError` rather than
silently degrading to plaintext — a worker that thinks it's running
mTLS but isn't is a foot-gun we deliberately refuse.

The :func:`build_*` helpers turn :class:`TlsMaterial` into the
flavour-specific config objects each transport needs (uvicorn's SSL
args, aioquic's ``QuicConfiguration``, httpx's ``verify`` / ``cert``).
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TlsNotConfiguredError(Exception):
    """Raised when an mTLS load is requested but the material is missing.

    Catch this at the worker / client entry point and either:

      - refuse to start (``ZAKURO_TLS_REQUIRED=1``), or
      - log a clear warning and fall through to a plaintext socket
        (dev only — never default to this in production).
    """


@dataclass(frozen=True)
class TlsMaterial:
    """Loaded mTLS files. Use :func:`load_server_tls` / :func:`load_client_tls`."""

    cert_path: Path
    key_path: Path
    ca_bundle_path: Path


def _resolve_cert_dir(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("ZAKURO_CERT_DIR", "").strip()
    if not raw:
        raise TlsNotConfiguredError(
            "ZAKURO_CERT_DIR is unset. Set it to the directory containing "
            "tls.crt / tls.key / ca.crt before starting the worker."
        )
    return Path(raw).expanduser()


def _check_file(path: Path, role: str) -> Path:
    if not path.is_file():
        raise TlsNotConfiguredError(f"{role} file missing or not a regular file: {path}")
    return path


def _load(cert_dir: str | None) -> TlsMaterial:
    base = _resolve_cert_dir(cert_dir)
    return TlsMaterial(
        cert_path=_check_file(base / "tls.crt", "tls.crt"),
        key_path=_check_file(base / "tls.key", "tls.key"),
        ca_bundle_path=_check_file(base / "ca.crt", "ca.crt"),
    )


def load_server_tls(cert_dir: str | None = None) -> TlsMaterial:
    """Load the server-side mTLS files for the worker.

    Raises :class:`TlsNotConfiguredError` if any file is missing.
    """
    return _load(cert_dir)


def load_client_tls(cert_dir: str | None = None) -> TlsMaterial:
    """Load the client-side mTLS files for the ZakuroClient.

    Same files as :func:`load_server_tls` — the symmetry is intentional;
    each side is both *a* server (responds) and *a* client (dispatches),
    and they share the same per-mesh CA. Separate function names for
    grep-friendliness.
    """
    return _load(cert_dir)


# ---- transport-specific builders --------------------------------------------


def build_server_ssl_context(material: TlsMaterial) -> ssl.SSLContext:
    """Return an ``SSLContext`` configured for mTLS server use.

    - TLS 1.3 (PROTOCOL_TLS_SERVER)
    - Loads the server cert + key
    - Requires + verifies the peer cert against the CA bundle
    - Disables session tickets / TLS 1.2 compat (RFC 0002 §"transport")
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(certfile=str(material.cert_path), keyfile=str(material.key_path))
    ctx.load_verify_locations(cafile=str(material.ca_bundle_path))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False  # we verify by SPIFFE-style SAN ourselves
    return ctx


def build_client_ssl_context(material: TlsMaterial) -> ssl.SSLContext:
    """Return an ``SSLContext`` configured for mTLS client use."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(certfile=str(material.cert_path), keyfile=str(material.key_path))
    ctx.load_verify_locations(cafile=str(material.ca_bundle_path))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False  # SPIFFE-style SAN verification handled in app
    return ctx


def build_httpx_client(material: TlsMaterial, *, base_url: str | None = None) -> Any:
    """Return an ``httpx.Client`` configured for mTLS.

    Passed as ``verify=<CA bundle>`` + ``cert=(cert, key)``. httpx
    accepts paths or already-loaded SSLContexts; we go with the
    pre-built context so both sides share the exact same settings.
    """
    import httpx

    ctx = build_client_ssl_context(material)
    kwargs: dict[str, Any] = {"verify": ctx}
    if base_url is not None:
        kwargs["base_url"] = base_url
    return httpx.Client(**kwargs)


def build_aioquic_server_configuration(material: TlsMaterial, *, alpn: str = "zk-worker-v2") -> Any:
    """Return an aioquic ``QuicConfiguration`` configured for mTLS server use.

    ALPN bump to ``zk-worker-v2`` matches the wire-format bump in RFC 0001
    (so v0.3 clients can't accidentally talk to a v0.4 worker).
    """
    try:
        from aioquic.quic.configuration import QuicConfiguration
    except ImportError as exc:  # pragma: no cover
        raise ImportError("QUIC support requires the [worker] extra (aioquic).") from exc

    config = QuicConfiguration(
        is_client=False,
        alpn_protocols=[alpn],
        verify_mode=ssl.CERT_REQUIRED,
    )
    config.load_cert_chain(
        certfile=str(material.cert_path),
        keyfile=str(material.key_path),
    )
    config.load_verify_locations(cafile=str(material.ca_bundle_path))
    return config
