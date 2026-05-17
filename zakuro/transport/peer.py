"""Peer-identity extraction for incoming requests (#115, RFC 0002).

Once mTLS terminates at the worker (or at an ingress in front of it),
the *peer* identity is carried by the X.509 client cert. This module
extracts that identity into a typed :class:`PeerIdentity` so the auth
middleware can refuse a cert from the wrong namespace before reading
any request body.

Supported transports
--------------------

1. **Direct mTLS** (uvicorn terminates TLS) — the peer cert lands in
   ``request.scope["extensions"]["tls.transport"]`` as an
   :class:`ssl.SSLObject` and we read it via
   :func:`extract_peer_from_request`.
2. **Ingress + X-Forwarded-Client-Cert** (RFC 9440) — the ingress
   forwards the verified cert as a header. We parse it via
   :func:`parse_x_forwarded_client_cert`.

Both paths return a :class:`PeerIdentity`; callers should not have
to know which transport produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_XFCC_PAIR_RE = re.compile(r'(\w+)=("([^"\\]|\\.)*"|[^;,]*)')


@dataclass(frozen=True)
class PeerIdentity:
    """Validated peer identity extracted from an inbound mTLS connection."""

    subject_cn: str | None
    """The client cert's CommonName, when present."""
    spiffe_id: str | None
    """The first SPIFFE-format URI SAN (``spiffe://…``), when present."""
    dns_sans: tuple[str, ...]
    """Any DNS SANs on the cert. Order preserved."""

    @property
    def is_anonymous(self) -> bool:
        """True iff no cert material was extracted."""
        return self.subject_cn is None and self.spiffe_id is None and not self.dns_sans


_ANONYMOUS = PeerIdentity(subject_cn=None, spiffe_id=None, dns_sans=())


def _from_ssl_object(obj: Any) -> PeerIdentity:
    """Extract a :class:`PeerIdentity` from an :class:`ssl.SSLObject`-like object.

    Accepts any object exposing ``getpeercert(binary_form=False)`` and
    ``getpeercert()`` returning the stdlib's parsed-cert dict.
    """
    try:
        cert = obj.getpeercert()
    except (ValueError, AttributeError):
        return _ANONYMOUS
    if not cert:
        return _ANONYMOUS
    subject_cn: str | None = None
    for rdn in cert.get("subject", ()):  # tuple of tuples
        for key, value in rdn:
            if key == "commonName":
                subject_cn = value
                break
    spiffe_id: str | None = None
    dns_sans: list[str] = []
    for kind, value in cert.get("subjectAltName", ()):
        if kind.lower() == "uri" and value.startswith("spiffe://"):
            spiffe_id = spiffe_id or value
        elif kind.lower() == "dns":
            dns_sans.append(value)
    return PeerIdentity(subject_cn=subject_cn, spiffe_id=spiffe_id, dns_sans=tuple(dns_sans))


def extract_peer_from_request(request: Any) -> PeerIdentity:
    """Extract the peer identity from a Starlette/FastAPI request.

    Tries direct-mTLS first (``scope["extensions"]["tls.transport"]``),
    falls back to the ``X-Forwarded-Client-Cert`` header (RFC 9440)
    when behind an ingress. Returns an anonymous identity if neither
    path produces material — the caller decides whether to refuse.
    """
    scope = getattr(request, "scope", None) or {}
    tls_obj = (scope.get("extensions") or {}).get("tls.transport")
    if tls_obj is not None:
        ident = _from_ssl_object(tls_obj)
        if not ident.is_anonymous:
            return ident
    header = request.headers.get("x-forwarded-client-cert", "")
    if header:
        return parse_x_forwarded_client_cert(header)
    return _ANONYMOUS


def parse_x_forwarded_client_cert(value: str) -> PeerIdentity:
    """Parse an RFC 9440 ``X-Forwarded-Client-Cert`` header value.

    The header carries one or more comma-separated cert entries, each a
    semicolon-separated list of key=value pairs (``Subject``, ``URI``,
    ``DNS``, ``Hash`` etc.). We honour the first entry only — RFC 9440
    states the first cert is the *direct* peer; subsequent entries are
    chain ancestors.
    """
    first_entry = value.split(",", 1)[0]
    subject_cn: str | None = None
    spiffe_id: str | None = None
    dns_sans: list[str] = []
    for match in _XFCC_PAIR_RE.finditer(first_entry):
        key = match.group(1)
        raw_value = match.group(2)
        # Strip enclosing double quotes when present
        if raw_value.startswith('"') and raw_value.endswith('"'):
            raw_value = raw_value[1:-1].replace('\\"', '"')
        lk = key.lower()
        if lk == "subject":
            for cn_match in re.finditer(r"CN=([^,]+)", raw_value):
                subject_cn = cn_match.group(1).strip()
                break
        elif lk == "uri" and raw_value.startswith("spiffe://"):
            spiffe_id = spiffe_id or raw_value.strip()
        elif lk == "dns":
            dns_sans.append(raw_value.strip())
    return PeerIdentity(subject_cn=subject_cn, spiffe_id=spiffe_id, dns_sans=tuple(dns_sans))
