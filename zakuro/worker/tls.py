"""Opt-in mutual TLS for the worker listeners (``ZAKURO_CERT_DIR``, ADR 0013).

When ``ZAKURO_CERT_DIR`` is set, the HTTP (uvicorn) and QUIC (aioquic)
listeners terminate TLS themselves and *require* a client certificate signed
by the CA bundle. When it is unset, nothing here is used and the listeners
behave exactly as before.

Files expected in the directory (all PEM):

* ``tls.crt`` -- this worker's certificate
* ``tls.key`` -- its private key (unencrypted)
* ``ca.crt``  -- CA bundle used to verify *client* certificates

A set-but-incomplete directory raises :class:`~zakuro.worker.posture.StartupConfigError`
rather than silently serving plaintext.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zakuro.worker.posture import StartupConfigError

QUIC_MTLS_ALPN = "zk-worker"


@dataclass(frozen=True)
class TlsMaterial:
    cert_path: Path
    key_path: Path
    ca_path: Path


def cert_dir() -> str | None:
    """Return ``ZAKURO_CERT_DIR`` if set and non-blank, else ``None``."""
    raw = os.environ.get("ZAKURO_CERT_DIR", "").strip()
    return raw or None


def mtls_enabled() -> bool:
    return cert_dir() is not None


def load_server_tls(directory: str | None = None) -> TlsMaterial:
    """Resolve and validate the server cert, key and client-CA files."""
    raw = directory or cert_dir()
    if not raw:
        raise StartupConfigError("ZAKURO_CERT_DIR is unset")
    base = Path(raw).expanduser()
    paths = []
    for name in ("tls.crt", "tls.key", "ca.crt"):
        p = base / name
        if not p.is_file():
            raise StartupConfigError(f"ZAKURO_CERT_DIR={base}: missing required file {name}")
        paths.append(p)
    return TlsMaterial(*paths)


def uvicorn_ssl_kwargs(material: TlsMaterial | None = None) -> dict[str, Any]:
    """Keyword args for ``uvicorn.run`` / ``uvicorn.Config``; ``{}`` when mTLS is off."""
    if material is None:
        if not mtls_enabled():
            return {}
        material = load_server_tls()
    return {
        "ssl_certfile": str(material.cert_path),
        "ssl_keyfile": str(material.key_path),
        "ssl_ca_certs": str(material.ca_path),
        "ssl_cert_reqs": int(ssl.CERT_REQUIRED),
    }


def apply_quic_mtls(config: Any, material: TlsMaterial | None = None) -> Any:
    """Load the server cert/key and CA into an aioquic server ``QuicConfiguration``.

    ``verify_mode=CERT_REQUIRED`` alone is **not** enough: aioquic's server
    never sends a CertificateRequest nor validates a client chain. Each
    accepted connection must also be passed through :func:`require_quic_client_cert`.
    """
    material = material or load_server_tls()
    config.load_cert_chain(str(material.cert_path), str(material.key_path))
    config.load_verify_locations(cafile=str(material.ca_path))
    config.verify_mode = ssl.CERT_REQUIRED
    return config


def require_quic_client_cert(quic: Any, material: TlsMaterial | None = None) -> None:
    """Make one server-side aioquic ``QuicConnection`` demand a CA-signed client cert.

    aioquic only requests a client certificate behind a private test flag
    (``tls._request_client_certificate``) and never verifies the chain on the
    server. We turn the flag on when the TLS context is created and verify the
    peer chain against ``ca.crt`` (rejecting an absent certificate) before the
    handshake can finish. aioquic validates the CertificateVerify signature
    itself, which proves possession of the client key. Any failure aborts the
    handshake, and an aioquic internals change fails closed (the handshake
    errors) rather than open.
    """
    from aioquic import tls as aio_tls

    material = material or load_server_tls()
    ca_file = str(material.ca_path)
    orig_initialize = quic._initialize

    def _initialize(peer_cid: bytes) -> None:
        orig_initialize(peer_cid)
        ctx = quic.tls
        ctx._request_client_certificate = True
        orig_finished = ctx._server_handle_finished

        def _server_handle_finished(input_buf: Any, output_buf: Any) -> None:
            peer = ctx._peer_certificate
            if peer is None:
                raise aio_tls.AlertHandshakeFailure("client certificate required")
            aio_tls.verify_certificate(
                certificate=peer,
                chain=list(ctx._peer_certificate_chain),
                cafile=ca_file,
            )
            orig_finished(input_buf, output_buf)

        ctx._server_handle_finished = _server_handle_finished

    quic._initialize = _initialize
