"""Tests for peer-identity extraction (#115, RFC 0002)."""

from __future__ import annotations

from typing import Any

from zakuro.transport.peer import (
    PeerIdentity,
    extract_peer_from_request,
    parse_x_forwarded_client_cert,
)


class _StubSSLObject:
    def __init__(self, cert: dict[str, Any] | None) -> None:
        self._cert = cert

    def getpeercert(self) -> dict[str, Any] | None:
        return self._cert


class _StubRequest:
    def __init__(
        self, *, tls_obj: Any | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.scope: dict[str, Any] = {"extensions": {"tls.transport": tls_obj}} if tls_obj else {}
        self.headers = headers or {}


def test_extract_peer_from_direct_mtls() -> None:
    cert = {
        "subject": ((("commonName", "tenant-acme/worker-1"),),),
        "subjectAltName": (
            ("URI", "spiffe://zakuro-ai.com/ns/acme/sa/worker"),
            ("DNS", "worker-1.acme.zakuro.local"),
        ),
    }
    ident = extract_peer_from_request(_StubRequest(tls_obj=_StubSSLObject(cert)))
    assert ident.subject_cn == "tenant-acme/worker-1"
    assert ident.spiffe_id == "spiffe://zakuro-ai.com/ns/acme/sa/worker"
    assert ident.dns_sans == ("worker-1.acme.zakuro.local",)


def test_extract_peer_falls_through_to_xfcc_header() -> None:
    header = (
        'Subject="CN=tenant-acme/worker-1";'
        "URI=spiffe://zakuro-ai.com/ns/acme/sa/worker;"
        "DNS=worker-1.acme.zakuro.local"
    )
    ident = extract_peer_from_request(_StubRequest(headers={"x-forwarded-client-cert": header}))
    assert ident.subject_cn == "tenant-acme/worker-1"
    assert ident.spiffe_id == "spiffe://zakuro-ai.com/ns/acme/sa/worker"
    assert "worker-1.acme.zakuro.local" in ident.dns_sans


def test_extract_peer_returns_anonymous_when_no_material() -> None:
    ident = extract_peer_from_request(_StubRequest())
    assert ident.is_anonymous


def test_parse_xfcc_takes_first_entry_only() -> None:
    # Two entries — direct peer first, chain ancestor second
    header = 'Subject="CN=direct-peer";URI=spiffe://zakuro-ai.com/ns/a/sa/w,Subject="CN=ca-cert"'
    ident = parse_x_forwarded_client_cert(header)
    assert ident.subject_cn == "direct-peer"


def test_parse_xfcc_ignores_non_spiffe_uris() -> None:
    header = 'Subject="CN=peer";URI=https://example.com'
    ident = parse_x_forwarded_client_cert(header)
    assert ident.spiffe_id is None


def test_peer_identity_is_anonymous() -> None:
    anon = PeerIdentity(subject_cn=None, spiffe_id=None, dns_sans=())
    named = PeerIdentity(subject_cn="cn", spiffe_id=None, dns_sans=())
    spiffe = PeerIdentity(subject_cn=None, spiffe_id="spiffe://", dns_sans=())
    assert anon.is_anonymous
    assert not named.is_anonymous
    assert not spiffe.is_anonymous
