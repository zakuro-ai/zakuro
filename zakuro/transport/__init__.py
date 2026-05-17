"""mTLS transport surface for Zakuro (#115, RFC 0002).

This package handles certificate loading + peer-identity extraction
for both ends of every transport (QUIC and HTTP). It is the substrate
on which the per-request JWT layer (:mod:`zakuro.auth`) builds:

  - mTLS authenticates the *peer* (cert subject = SPIFFE-style ID);
  - JWT authenticates the *request* (per-tenant scope).

When the env var ``ZAKURO_CERT_DIR`` is unset, the loaders return
``None`` so the worker still runs without TLS in dev — but
:func:`load_server_tls_context` warns and the integration tests
expect mTLS in CI.

Forthcoming work
----------------

- Phase 2 PR wires the contexts into ``zakuro.worker.server`` (uvicorn
  SSL args), ``zakuro.worker.quic_server`` (aioquic QuicConfiguration),
  and the client's :class:`zakuro.client.ZakuroClient` (httpx + aioquic).
- Once cert rotation under load is verified, the gossip-handshake (RFC
  0008) re-issuance path lands as a third PR.
"""

from zakuro.transport.peer import (
    PeerIdentity,
    extract_peer_from_request,
    parse_x_forwarded_client_cert,
)
from zakuro.transport.tls import (
    TlsMaterial,
    TlsNotConfiguredError,
    build_aioquic_server_configuration,
    build_httpx_client,
    load_client_tls,
    load_server_tls,
)

__all__ = [
    "PeerIdentity",
    "TlsMaterial",
    "TlsNotConfiguredError",
    "build_aioquic_server_configuration",
    "build_httpx_client",
    "extract_peer_from_request",
    "load_client_tls",
    "load_server_tls",
    "parse_x_forwarded_client_cert",
]
