"""JWT guard for QUIC frames (ADR 0013, #84).

The QUIC wire is ``op(1) | len(4) | payload``. To carry a bearer token without
changing the framing, a client sets the high bit of ``op`` (:data:`AUTH_FLAG`)
and prefixes the payload with ``token_len(2, big-endian) | token``. Workers with
``ZAKURO_AUTH_REQUIRED`` unset accept both forms (the token is ignored), so
existing clients are unchanged.
"""

from __future__ import annotations

import logging

from zakuro.auth.jwt import JwtError, authorize_token
from zakuro.auth.middleware import failure_reason
from zakuro.worker.posture import is_auth_required

logger = logging.getLogger(__name__)

AUTH_FLAG = 0x80
STAT_UNAUTHORIZED = 3

# Scope per opcode. ``None`` = open (liveness probe, as ``/health`` over HTTP).
_SCOPES: dict[int, str | None] = {1: "exec:fn", 2: "worker:info", 3: None}


class FrameAuthError(Exception):
    """The frame is not authorised; reject before execution."""


def pack_token(token: str, payload: bytes) -> bytes:
    """Client side: prefix *payload* with the token (pair with ``op | AUTH_FLAG``)."""
    raw = token.encode()
    if len(raw) > 0xFFFF:
        raise ValueError("token too long for a QUIC frame")
    return len(raw).to_bytes(2, "big") + raw + payload


def authorize_frame(op: int, payload: bytes) -> tuple[int, bytes]:
    """Return ``(base_op, payload)`` with any token stripped, or raise :class:`FrameAuthError`."""
    base = op & ~AUTH_FLAG & 0xFF
    token: str | None = None
    if op & AUTH_FLAG:
        token_len = -1 if len(payload) < 2 else int.from_bytes(payload[:2], "big")
        if token_len < 0 or len(payload) < 2 + token_len:
            logger.warning("auth refused: malformed_frame")
            raise FrameAuthError  # a malformed token envelope is never executable
        token = payload[2 : 2 + token_len].decode("utf-8", "replace")
        payload = payload[2 + token_len :]
    if not is_auth_required():
        return base, payload
    scope = _SCOPES.get(base)
    if base not in _SCOPES:
        return base, payload  # unknown opcode -> normal protocol error, nothing executes
    if scope is None:
        return base, payload
    if token is None:
        logger.warning("auth refused: missing_token")
        raise FrameAuthError
    try:
        authorize_token(token, scope)
    except JwtError as exc:
        logger.warning("auth refused: %s", failure_reason(exc))
        raise FrameAuthError from None
    return base, payload
