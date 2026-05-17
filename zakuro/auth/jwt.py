"""Ed25519 JWT verification + HMAC-key derivation (RFC 0002 / #116).

Design notes
------------

* The worker only ever **verifies** JWTs — it never issues them. The
  broker (zc) is the issuer (RFC 0002 §"JWT issuance"). The worker's
  public verification key is bundled at build time + can be rotated
  via the gossip handshake (RFC 0008).
* Verification uses PyJWT with `algorithms=["EdDSA"]` exclusively to
  block the classic alg-confusion attacks (`{"alg": "none"}`,
  `{"alg": "HS256"}` against a public key, etc.).
* Required claims: ``aud``, ``exp``, ``nbf``, ``jti``, ``tenant_id``,
  ``worker_id``, ``scopes``. Verification fails closed if any are
  absent or malformed.
* The HMAC key for the postcard envelope's signature (#117) is HKDF-
  derived from the JWT signing key by the broker; the worker receives
  it directly via the same channel that issued the JWT. The
  :func:`derive_payload_hmac_key` helper performs that HKDF locally so
  unit tests don't have to spin up a broker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    import jwt as _pyjwt
    from jwt.exceptions import (
        ExpiredSignatureError,
        InvalidAudienceError,
        InvalidSignatureError,
        InvalidTokenError,
        MissingRequiredClaimError,
    )
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "Verifying Zakuro JWTs requires the `[worker]` extra "
        "(PyJWT[crypto]). Install it with `pip install 'zakuro-ai[worker]'`."
    ) from exc

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_HMAC_KEY_LENGTH = 32
_HKDF_SALT = b"zakuro-wire-v1"


class JwtError(Exception):
    """Base for any JWT-related failure."""


class JwtExpiredError(JwtError):
    """Token's ``exp`` claim has passed."""


class JwtSignatureError(JwtError):
    """Token signature did not validate against the configured public key."""


class JwtScopeError(JwtError):
    """Token did not include the required scope."""


@dataclass(frozen=True)
class Claims:
    """The validated claims a Zakuro JWT carries."""

    tenant_id: str
    worker_id: str
    scopes: frozenset[str]
    jti: str
    exp: int
    nbf: int
    aud: str

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _public_key_from_env() -> bytes:
    """Read the broker's Ed25519 public key from ``ZAKURO_JWT_PUBLIC_KEY_PEM`` or its path.

    The env var may carry either the PEM string directly (multi-line) or a
    filesystem path. A missing value raises :class:`JwtSignatureError`
    rather than returning ``None`` — refusing all tokens is the right
    failure mode when the worker can't know who signed them.
    """
    raw = os.environ.get("ZAKURO_JWT_PUBLIC_KEY_PEM", "").strip()
    if not raw:
        raise JwtSignatureError(
            "ZAKURO_JWT_PUBLIC_KEY_PEM is unset — refusing all JWTs. Configure "
            "the worker with the broker's Ed25519 public key (PEM)."
        )
    if raw.startswith("-----BEGIN"):
        return raw.encode("utf-8")
    # treat as a path
    try:
        with open(raw, "rb") as f:
            return f.read()
    except OSError as exc:
        raise JwtSignatureError(f"could not read public key from {raw}: {exc}") from exc


def verify_jwt(
    token: str,
    *,
    expected_audience: str,
    expected_worker_id: str | None = None,
    public_key_pem: bytes | None = None,
    leeway_seconds: int = 0,
) -> Claims:
    """Verify a JWT and return its :class:`Claims`.

    Parameters
    ----------
    token
        The bearer token (no ``"Bearer "`` prefix).
    expected_audience
        Required ``aud`` claim. A token issued for another component
        (e.g. the dashboard) fails verification.
    expected_worker_id
        If set, the token's ``worker_id`` claim must match. Prevents
        replay against a different worker in the same tenant.
    public_key_pem
        Override the env-resolved public key. Test-only.
    leeway_seconds
        Clock-skew tolerance for ``exp`` / ``nbf``.

    Raises :class:`JwtSignatureError`, :class:`JwtExpiredError`, or
    :class:`JwtError` on any failure. Returns the parsed claims on
    success.
    """
    key = public_key_pem if public_key_pem is not None else _public_key_from_env()
    try:
        payload: dict[str, Any] = _pyjwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            audience=expected_audience,
            leeway=leeway_seconds,
            options={
                "require": ["exp", "nbf", "jti", "aud"],
                "verify_aud": True,
                "verify_exp": True,
                "verify_nbf": True,
            },
        )
    except ExpiredSignatureError as exc:
        raise JwtExpiredError(str(exc)) from exc
    except (InvalidSignatureError, InvalidTokenError, InvalidAudienceError) as exc:
        raise JwtSignatureError(str(exc)) from exc
    except MissingRequiredClaimError as exc:
        raise JwtError(str(exc)) from exc

    try:
        tenant_id = str(payload["tenant_id"])
        worker_id = str(payload["worker_id"])
        scopes_raw = payload["scopes"]
        if not isinstance(scopes_raw, (list, tuple)):
            raise JwtError("scopes claim must be an array")
        scopes = frozenset(str(s) for s in scopes_raw)
        jti = str(payload["jti"])
        exp = int(payload["exp"])
        nbf = int(payload["nbf"])
        aud_claim = str(payload["aud"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JwtError(f"malformed claim set: {exc}") from exc

    if expected_worker_id is not None and worker_id != expected_worker_id:
        raise JwtSignatureError(
            f"token issued for worker_id={worker_id!r}, this worker is {expected_worker_id!r}"
        )

    return Claims(
        tenant_id=tenant_id,
        worker_id=worker_id,
        scopes=scopes,
        jti=jti,
        exp=exp,
        nbf=nbf,
        aud=aud_claim,
    )


def derive_payload_hmac_key(*, jwt_signing_key: bytes, tenant_id: str) -> bytes:
    """HKDF-derive the per-tenant key used to verify postcard-envelope HMACs.

    Matches the Rust crate's expected derivation:
    ``HKDF-SHA256(jwt_signing_key, salt="zakuro-wire-v1", info=f"payload:{tenant_id}")``.

    This is called by the broker per-tenant + bundled with the dispatch;
    the worker also derives it from its own copy of the broker's signing
    key when verifying. Same algorithm, same result.
    """
    if not jwt_signing_key:
        raise JwtError("jwt_signing_key must not be empty")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_HMAC_KEY_LENGTH,
        salt=_HKDF_SALT,
        info=f"payload:{tenant_id}".encode(),
    )
    return hkdf.derive(jwt_signing_key)
