"""Gate for inbound ``/execute`` bodies: verify the signed envelope before unpickling.

With ``ZAKURO_WIRE=v1`` every body must be a postcard envelope whose HMAC
verifies against the per-tenant key derived from ``ZAKURO_HMAC_KEY`` /
``ZAKURO_HMAC_KEY_FILE``; only then is the inner cloudpickle blob handed to the
executor. Anything else raises :class:`EnvelopeRejectedError` (HTTP 401 / QUIC
error frame) with no detail, so the failure reason cannot aid enumeration.
With ``ZAKURO_WIRE`` unset the bytes pass through unchanged (legacy behaviour).
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from zakuro.wire import (
    WIRE_VERSION_V1,
    WIRE_VERSION_V2,
    Envelope,
    WireError,
    decode_envelope,
    decode_envelope_v2,
    peek_version,
    verify_hmac,
    verify_hmac_v2,
)
from zakuro.wire.keys import (
    derive_payload_hmac_key,
    derive_payload_hmac_key_v2,
    read_master_key,
    wire_mode,
)

DEFAULT_MAX_SKEW_S = 300
REPLAY_CACHE_CAP = 100_000


class EnvelopeRejectedError(WireError):
    """The envelope is invalid, unsigned, tampered, or the worker is misconfigured."""


class ReplayCache:
    """Bounded, thread-safe memory of ``(tenant_id, job_id)`` pairs seen in the window.

    Shared by the HTTP and QUIC servers (one worker process). Entries expire
    after the freshness window; at the cap the oldest entry is evicted.
    """

    def __init__(self, cap: int = REPLAY_CACHE_CAP) -> None:
        self._cap = cap
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_add(self, key: tuple[str, str], *, now: float, ttl: float) -> bool:
        """Record *key*; return False if it was already seen and is still live."""
        with self._lock:
            while self._seen:
                oldest = next(iter(self._seen))
                if self._seen[oldest] > now:
                    break
                del self._seen[oldest]
            if key in self._seen:
                return False
            self._seen[key] = now + ttl
            while len(self._seen) > self._cap:
                self._seen.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


_replay_cache = ReplayCache()


def _max_skew_s() -> float:
    raw = os.environ.get("ZAKURO_WIRE_MAX_SKEW_S", "").strip()
    try:
        value = float(raw) if raw else float(DEFAULT_MAX_SKEW_S)
    except ValueError:
        return float(DEFAULT_MAX_SKEW_S)
    return value if value > 0 else float(DEFAULT_MAX_SKEW_S)


def _accept_v1() -> bool:
    return os.environ.get("ZAKURO_WIRE_ACCEPT_V1", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _unwrap_v2(raw: bytes, master_key: bytes) -> bytes:
    env = decode_envelope_v2(raw)
    verify_hmac_v2(
        env,
        hmac_key=derive_payload_hmac_key_v2(master_key=master_key, tenant_id=env.tenant_id),
    )
    # Freshness and replay are checked only after the MAC verifies, so an
    # unauthenticated sender cannot fill the replay cache.
    now = time.time()
    skew = _max_skew_s()
    if abs(now - env.issued_at_ms / 1000.0) > skew:
        raise EnvelopeRejectedError("stale")
    if not _replay_cache.check_and_add((env.tenant_id, env.job_id), now=now, ttl=2 * skew):
        raise EnvelopeRejectedError("replay")
    return env.callable


def unwrap_payload(raw: bytes) -> bytes:
    """Return the inner cloudpickle bytes for the executor.

    Raises :class:`EnvelopeRejectedError` on any decode / HMAC / key failure
    under ``ZAKURO_WIRE=v1|v2``. Never calls ``cloudpickle``. Under ``v2`` the
    worker accepts v2 and (unless ``ZAKURO_WIRE_ACCEPT_V1=0``) v1 envelopes;
    v2 adds a freshness window and a replay cache (ZAK-0020).
    """
    mode = wire_mode()
    if not mode:
        return raw
    try:
        master_key = read_master_key()
        if mode == "v2":
            version = peek_version(raw)
            if version == WIRE_VERSION_V2:
                return _unwrap_v2(raw, master_key)
            if version != WIRE_VERSION_V1 or not _accept_v1():
                raise EnvelopeRejectedError("version")
        envelope: Envelope = decode_envelope(raw)
        verify_hmac(
            envelope,
            hmac_key=derive_payload_hmac_key(master_key=master_key, tenant_id=envelope.tenant_id),
        )
    except EnvelopeRejectedError:
        raise
    except Exception as exc:
        raise EnvelopeRejectedError(type(exc).__name__) from None
    return envelope.callable
