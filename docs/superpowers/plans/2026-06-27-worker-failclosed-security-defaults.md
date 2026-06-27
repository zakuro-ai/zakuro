# Worker Fail-Closed Security Net Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a Zakuro worker refuse to expose an unauthenticated remote-code-execution surface by default — without breaking any correctly-configured (or explicitly-opted-in) deployment.

**Architecture:** Add one small, dependency-light module, `zakuro/worker/posture.py`, that is the single source of truth for "is this worker safe to expose?" It exposes pure, env-driven predicates plus three operator-facing guards wired into every process entry point: (1) `enforce_bind_policy` refuses a non-loopback bind when no caller-authentication control is on, unless `ZAKURO_INSECURE_BIND=1`; (2) `validate_startup_config` fails fast on typo'd/inconsistent security env vars and on strict-wire-without-a-key; (3) a startup security banner makes the posture visible. A fourth change caps inbound payload size at the single `unwrap_payload` chokepoint shared by the HTTP and QUIC `/execute` paths.

**Tech Stack:** Python ≥3.10, FastAPI/uvicorn + aioquic (`[worker]` extra), pytest + pytest-cov, ruff, mypy, hatchling.

## Context (why this change, and why NOT the literal default-flip)

The worker's `/execute` boundary deserialises cloudpickle. Three controls can make that safe — JWT auth (`ZAKURO_AUTH_REQUIRED=1`, RFC 0002), HMAC-signed wire (`ZAKURO_WIRE=v1` + master key, RFC 0001 §"Step 5"), and mutual TLS (`ZAKURO_CERT_DIR` set → `cli.py` requires client certs). **All three are off by default** (`auth/middleware.py:59`, `worker/envelope.py:57`), so a worker started on the default `0.0.0.0:3960` (`worker/cli.py:40`, `worker/server.py:402`, `worker/quic_server.py:353`) with none of them set is an **unauthenticated RCE surface** — the exact class of `sakura-internal#9` (P0).

The obvious fix — "flip the defaults to on" — is **not safe to land in this repo alone**:
- The client never wraps envelopes. `client.py:83-107` posts raw cloudpickle (`content=payload`); there is no `ZAKURO_WIRE`/envelope code in `client.py` or `processors/`. The envelope docstring (`worker/envelope.py:139`) confirms client wrapping is "forthcoming." Flipping `ZAKURO_WIRE=v1` on the worker would reject 100% of current client traffic.
- Auth-required-by-default needs JWT issuance + client token-sending that does not exist yet on the SDK path, and would break every local/dev worker.
- The wire format is shared with the `zc` Rust broker; the flip must be coordinated cross-repo. The code comments call this "the v0.3 → v0.4 rollout."

So this plan delivers the **fail-closed *net*** that closes the accidental-exposure hole now, is non-breaking for secured or explicitly-opted-in deployments, and cleanly sets up the coordinated default-flip later. The literal default-flip is **explicitly deferred to a v0.4 cross-repo change** (see "Deferred / follow-up" at the end). This mirrors the existing in-repo fail-closed idiom `ZAKURO_TLS_REQUIRED` (`transport/tls.py:35`).

## Global Constraints

- Target Python **≥ 3.10**; `ruff target-version = py310`, `mypy python_version = 3.10` (`pyproject.toml:114,122`). All new code must pass `ruff check` and `mypy zakuro/`.
- New module **must not import FastAPI/uvicorn/aioquic at module load** — it is imported on operator entry paths and unit-tested without the `[worker]` server running. Heavy/intra-package imports are done lazily inside functions.
- **DRY:** reuse the existing canonical predicates `zakuro.worker.envelope.is_wire_strict` and `zakuro.worker.envelope._read_master_key`; do not duplicate their env semantics.
- New env vars use the `ZAKURO_` prefix: `ZAKURO_INSECURE_BIND` (escape hatch), `ZAKURO_MAX_PAYLOAD_BYTES` (DoS cap, default 256 MiB).
- **Non-breaking guarantee:** loopback binds, and binds where auth/wire/mTLS is enabled, behave exactly as before. Only an *insecure non-loopback* bind changes behaviour (now refused unless `ZAKURO_INSECURE_BIND=1`).
- Tests live under `tests/` (flat for worker-level tests, matching `tests/test_worker_runner.py`); use `monkeypatch.setenv/delenv`. Run with `python -m pytest tests/ -v`.
- Conventional-commit messages (`feat:`/`fix:`/`test:`/`docs:`). Configure git author **CADIC Jean Maximilien `<jmcadic.me@gmail.com>`** before committing. End each commit message with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: Posture module — predicates + scaffold

**Files:**
- Create: `zakuro/worker/posture.py`
- Test: `tests/test_posture.py`

**Interfaces:**
- Produces: `InsecureBindError(RuntimeError)`, `StartupConfigError(RuntimeError)`; `is_auth_required() -> bool`, `is_wire_strict() -> bool`, `is_mtls_enabled() -> bool`, `is_caller_authenticated() -> bool`, `is_loopback(host: str) -> bool`, `insecure_bind_allowed() -> bool`, `max_payload_bytes() -> int`, `security_posture(host: str | None = None, port: int | None = None) -> dict[str, object]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_posture.py
import pytest

from zakuro.worker import posture as P


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "ZAKURO_AUTH_REQUIRED", "ZAKURO_WIRE", "ZAKURO_CERT_DIR",
        "ZAKURO_INSECURE_BIND", "ZAKURO_HMAC_KEY", "ZAKURO_HMAC_KEY_FILE",
        "ZAKURO_MAX_PAYLOAD_BYTES",
    ):
        monkeypatch.delenv(k, raising=False)


def test_default_posture_is_unauthenticated():
    assert P.is_auth_required() is False
    assert P.is_wire_strict() is False
    assert P.is_mtls_enabled() is False
    assert P.is_caller_authenticated() is False


def test_auth_flag_truthy(monkeypatch):
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    assert P.is_auth_required() is True
    assert P.is_caller_authenticated() is True


def test_wire_strict_counts_as_authenticated(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    assert P.is_wire_strict() is True
    assert P.is_caller_authenticated() is True


def test_mtls_counts_as_authenticated(monkeypatch):
    monkeypatch.setenv("ZAKURO_CERT_DIR", "/etc/zakuro/certs")
    assert P.is_mtls_enabled() is True
    assert P.is_caller_authenticated() is True


def test_loopback_detection():
    assert P.is_loopback("127.0.0.1") is True
    assert P.is_loopback("::1") is True
    assert P.is_loopback("LOCALHOST") is True
    assert P.is_loopback("0.0.0.0") is False
    assert P.is_loopback("10.13.13.21") is False


def test_max_payload_default_is_256_mib():
    assert P.max_payload_bytes() == 256 * 1024 * 1024


def test_security_posture_includes_host_and_port():
    snap = P.security_posture(host="0.0.0.0", port=3960)
    assert snap["host"] == "0.0.0.0"
    assert snap["port"] == 3960
    assert snap["loopback"] is False
    assert snap["caller_authenticated"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_posture.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zakuro.worker.posture'`.

- [ ] **Step 3: Write minimal implementation**

```python
# zakuro/worker/posture.py
"""Security posture + fail-closed bind policy for the Zakuro worker.

Single source of truth for "is this worker safe to expose on the network?".
The worker's ``/execute`` boundary deserialises cloudpickle. Three independent
controls can authenticate the caller and make that safe:

* JWT auth   — ``ZAKURO_AUTH_REQUIRED=1`` (RFC 0002)
* HMAC wire  — ``ZAKURO_WIRE=v1`` plus a master key (RFC 0001 §"Step 5")
* mutual TLS — ``ZAKURO_CERT_DIR`` set (``cli.py`` then requires client certs)

When none is enabled, an ``/execute`` listener on a routable interface is an
unauthenticated remote-code-execution surface. :func:`enforce_bind_policy`
refuses such a bind to a non-loopback address unless the operator accepts the
risk with ``ZAKURO_INSECURE_BIND=1``. This mirrors the existing
``ZAKURO_TLS_REQUIRED`` fail-closed idiom in :mod:`zakuro.transport.tls`.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("zakuro.worker.posture")

_TRUE = {"1", "true", "yes"}
_FALSE = {"", "0", "false", "no"}
_WIRE_STRICT = {"v1"}
_WIRE_OFF = {"", "legacy", "off", "0", "none"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_DEFAULT_MAX_PAYLOAD = 256 * 1024 * 1024  # 256 MiB


class InsecureBindError(RuntimeError):
    """Raised when an insecure worker would bind a routable interface."""


class StartupConfigError(RuntimeError):
    """Raised when security-related env vars are inconsistent or typo'd."""


def is_auth_required() -> bool:
    """True iff ``ZAKURO_AUTH_REQUIRED`` is a recognised truthy value."""
    return os.environ.get("ZAKURO_AUTH_REQUIRED", "").strip().lower() in _TRUE


def is_wire_strict() -> bool:
    """Re-use the canonical predicate so the two never drift (DRY)."""
    from zakuro.worker.envelope import is_wire_strict as _impl

    return _impl()


def is_mtls_enabled() -> bool:
    """True iff a cert dir is configured — ``cli.py`` then requires client certs."""
    return bool(os.environ.get("ZAKURO_CERT_DIR", "").strip())


def is_caller_authenticated() -> bool:
    """True iff at least one control authenticates the ``/execute`` caller."""
    return is_auth_required() or is_wire_strict() or is_mtls_enabled()


def is_loopback(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK


def insecure_bind_allowed() -> bool:
    return os.environ.get("ZAKURO_INSECURE_BIND", "").strip().lower() in _TRUE


def max_payload_bytes() -> int:
    """Inbound ``/execute`` body cap. Raises :class:`StartupConfigError` if set
    to a non-positive / non-integer value."""
    raw = os.environ.get("ZAKURO_MAX_PAYLOAD_BYTES", "").strip()
    if not raw:
        return _DEFAULT_MAX_PAYLOAD
    try:
        val = int(raw)
    except ValueError as exc:
        raise StartupConfigError(
            f"ZAKURO_MAX_PAYLOAD_BYTES must be an integer, got {raw!r}"
        ) from exc
    if val <= 0:
        raise StartupConfigError("ZAKURO_MAX_PAYLOAD_BYTES must be positive")
    return val


def security_posture(host: str | None = None, port: int | None = None) -> dict[str, object]:
    """Structured snapshot of the worker's security posture for logging."""
    snap: dict[str, object] = {
        "auth_required": is_auth_required(),
        "wire_strict": is_wire_strict(),
        "mtls": is_mtls_enabled(),
        "caller_authenticated": is_caller_authenticated(),
        "max_payload_bytes": max_payload_bytes(),
    }
    if host is not None:
        snap["host"] = host
        snap["loopback"] = is_loopback(host)
    if port is not None:
        snap["port"] = port
    return snap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_posture.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add zakuro/worker/posture.py tests/test_posture.py
git commit -m "feat(worker): add security-posture predicates module

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Startup config validation (fail fast on typos / missing key)

**Files:**
- Modify: `zakuro/worker/posture.py` (add `validate_startup_config`)
- Test: `tests/test_posture.py` (extend)

**Interfaces:**
- Consumes: `is_wire_strict`, `max_payload_bytes`, `StartupConfigError` (Task 1); `zakuro.worker.envelope._read_master_key` + `EnvelopeRejectedError` (existing, `worker/envelope.py:62,44`).
- Produces: `validate_startup_config() -> None`.

Rationale: today a typo like `ZAKURO_WIRE=true` or `ZAKURO_AUTH_REQUIRED=tru` silently fails **open** (insecure), and a strict worker with no key fails only on the first request (`unwrap_payload` → `_read_master_key`). Validate at startup instead.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_posture.py  (append)
def test_validate_ok_with_defaults():
    P.validate_startup_config()  # no raise


def test_validate_rejects_wire_typo(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "true")  # not 'v1' nor a known off-token
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()


def test_validate_accepts_known_off_tokens(monkeypatch):
    for tok in ("", "legacy", "off"):
        monkeypatch.setenv("ZAKURO_WIRE", tok)
        P.validate_startup_config()  # no raise


def test_validate_rejects_auth_typo(monkeypatch):
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "tru")
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()


def test_validate_strict_without_key_fails(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")  # strict but no ZAKURO_HMAC_KEY*
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()


def test_validate_strict_with_key_ok(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", "00" * 32)  # valid hex
    P.validate_startup_config()  # no raise


def test_validate_rejects_bad_payload_cap(monkeypatch):
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "abc")
    with pytest.raises(P.StartupConfigError):
        P.validate_startup_config()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_posture.py -k validate -v`
Expected: FAIL — `AttributeError: module 'zakuro.worker.posture' has no attribute 'validate_startup_config'`.

- [ ] **Step 3: Write minimal implementation**

```python
# zakuro/worker/posture.py  (append)
def validate_startup_config() -> None:
    """Fail fast on inconsistent security env vars.

    Raises :class:`StartupConfigError` when a security toggle is set to an
    unrecognised value (which would silently fail *open*), when the payload
    cap is malformed, or when strict wire is on without a usable master key.
    """
    wire = os.environ.get("ZAKURO_WIRE", "").strip().lower()
    if wire not in _WIRE_STRICT and wire not in _WIRE_OFF:
        raise StartupConfigError(
            f"ZAKURO_WIRE={wire!r} is not recognised. Use 'v1' for strict mode, "
            "or leave it unset for legacy mode — a typo here silently disables "
            "wire security."
        )

    auth = os.environ.get("ZAKURO_AUTH_REQUIRED", "").strip().lower()
    if auth not in _TRUE and auth not in _FALSE:
        raise StartupConfigError(
            f"ZAKURO_AUTH_REQUIRED={auth!r} is not recognised. Use '1' to require "
            "auth, or leave it unset to disable — a typo here silently disables auth."
        )

    # Eagerly validate the payload cap (raises StartupConfigError on garbage).
    max_payload_bytes()

    # Strict wire with no usable key must fail at boot, not on first request.
    if is_wire_strict():
        from zakuro.worker.envelope import EnvelopeRejectedError, _read_master_key

        try:
            _read_master_key()
        except EnvelopeRejectedError as exc:
            raise StartupConfigError(str(exc)) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_posture.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add zakuro/worker/posture.py tests/test_posture.py
git commit -m "feat(worker): validate security env vars at startup (fail closed on typos)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Fail-closed bind policy + `resolve_listener` + entry-point wiring

**Files:**
- Modify: `zakuro/worker/posture.py` (add `enforce_bind_policy`, `log_security_banner`, `resolve_listener`)
- Modify: `zakuro/worker/cli.py:63-127` (http + quic branches of `main`)
- Modify: `zakuro/worker/server.py:394-415` (secondary `main`)
- Modify: `zakuro/worker/quic_server.py:365-382` (`__main__` parser path; **not** the `run_quic_worker` library function)
- Modify: `docker/docker-compose.yml`, `docker/docker-compose.mesh.yml`, `docker/docker-compose.two-nodes.yml` (and any other compose file defining a worker service) — keep the dev mesh working under the new policy
- Test: `tests/test_posture.py` (extend)

**Interfaces:**
- Consumes: `is_caller_authenticated`, `is_loopback`, `insecure_bind_allowed`, `validate_startup_config`, `security_posture`, `InsecureBindError`, `StartupConfigError` (Tasks 1-2).
- Produces: `enforce_bind_policy(host: str) -> str`, `log_security_banner(host: str, port: int) -> None`, `resolve_listener(host: str, port: int) -> str` (runs validate → enforce → banner, returns the host to bind, raises `InsecureBindError`/`StartupConfigError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_posture.py  (append)
def test_insecure_nonloopback_bind_refused():
    with pytest.raises(P.InsecureBindError):
        P.enforce_bind_policy("0.0.0.0")


def test_loopback_bind_allowed_when_insecure():
    assert P.enforce_bind_policy("127.0.0.1") == "127.0.0.1"


def test_auth_required_allows_nonloopback(monkeypatch):
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    assert P.enforce_bind_policy("0.0.0.0") == "0.0.0.0"


def test_wire_strict_allows_nonloopback(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    assert P.enforce_bind_policy("0.0.0.0") == "0.0.0.0"


def test_explicit_override_allows_nonloopback(monkeypatch):
    monkeypatch.setenv("ZAKURO_INSECURE_BIND", "1")
    assert P.enforce_bind_policy("0.0.0.0") == "0.0.0.0"


def test_resolve_listener_refuses_insecure_nonloopback():
    with pytest.raises(P.InsecureBindError):
        P.resolve_listener("0.0.0.0", 3960)


def test_resolve_listener_ok_loopback():
    assert P.resolve_listener("127.0.0.1", 3960) == "127.0.0.1"


def test_banner_warns_when_exposed_with_override(monkeypatch, caplog):
    monkeypatch.setenv("ZAKURO_INSECURE_BIND", "1")
    with caplog.at_level("WARNING", logger="zakuro.worker.posture"):
        P.log_security_banner("0.0.0.0", 3960)
    assert any(r.levelname == "WARNING" for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_posture.py -k "bind or resolve or banner" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'enforce_bind_policy'`.

- [ ] **Step 3: Write minimal implementation**

```python
# zakuro/worker/posture.py  (append)
def enforce_bind_policy(host: str) -> str:
    """Return *host* if it is safe to bind, else raise :class:`InsecureBindError`.

    Safe when the caller is authenticated (auth / wire / mTLS), the bind is
    loopback-only, or the operator set ``ZAKURO_INSECURE_BIND=1`` (trusted-mesh
    escape hatch).
    """
    if is_caller_authenticated() or is_loopback(host) or insecure_bind_allowed():
        return host
    raise InsecureBindError(
        f"refusing to bind the Zakuro worker to {host!r}: no caller authentication "
        "is enabled, so /execute would be an unauthenticated remote-code-execution "
        "surface. Enable ONE of:\n"
        "  - ZAKURO_AUTH_REQUIRED=1                       (JWT auth, RFC 0002)\n"
        "  - ZAKURO_WIRE=v1 + ZAKURO_HMAC_KEY[_FILE]      (signed wire, RFC 0001)\n"
        "  - ZAKURO_CERT_DIR=<dir>                        (mutual TLS)\n"
        "  - bind loopback, e.g. --host 127.0.0.1\n"
        "  - ZAKURO_INSECURE_BIND=1                       (accept the risk on a "
        "trusted, network-isolated mesh)"
    )


def log_security_banner(host: str, port: int) -> None:
    """Emit a one-line summary of the worker's security posture at startup."""
    snap = security_posture(host=host, port=port)
    if not snap["caller_authenticated"] and not snap["loopback"]:
        logger.warning(
            "Zakuro worker EXPOSED without caller authentication on %s:%s "
            "(ZAKURO_INSECURE_BIND override) — relying on network isolation. "
            "posture=%s",
            host, port, snap,
        )
    else:
        logger.info("Zakuro worker security posture on %s:%s — %s", host, port, snap)


def resolve_listener(host: str, port: int) -> str:
    """Validate config, enforce the bind policy, log the banner; return the host.

    Raises :class:`StartupConfigError` or :class:`InsecureBindError`; entry
    points catch these and ``sys.exit`` with the message.
    """
    validate_startup_config()
    host = enforce_bind_policy(host)
    log_security_banner(host, port)
    return host
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_posture.py -v`
Expected: PASS.

- [ ] **Step 5: Wire `resolve_listener` into the HTTP entry point (`zakuro/worker/cli.py`)**

In `main()`, replace the `port = ...` line and the lines up to `uvicorn.run(...)` so the listener is resolved first. The edited region (currently `cli.py:99-127`) becomes:

```python
    port = args.port if args.port is not None else int(os.environ.get("ZAKURO_PORT", "3960"))

    from zakuro.worker.posture import (
        InsecureBindError,
        StartupConfigError,
        resolve_listener,
    )

    try:
        host = resolve_listener(args.host, port)
    except (InsecureBindError, StartupConfigError) as exc:
        sys.exit(str(exc))

    # mTLS rollout (#115 Phase 2): when ZAKURO_CERT_DIR is set, load
    # server cert + private key + CA bundle and pass to uvicorn so the
    # listener terminates TLS itself. When the env var is unset, the
    # server stays on plaintext HTTP (dev / CI / behind-a-TLS-ingress).
    ssl_kwargs: dict[str, object] = {}
    if os.environ.get("ZAKURO_CERT_DIR", "").strip():
        from zakuro.transport import load_server_tls

        material = load_server_tls()
        ssl_kwargs = {
            "ssl_certfile": str(material.cert_path),
            "ssl_keyfile": str(material.key_path),
            "ssl_ca_certs": str(material.ca_bundle_path),
            "ssl_cert_reqs": 2,
        }

    uvicorn.run(
        "zakuro.worker.server:app",
        host=host,
        port=port,
        reload=False,
        **ssl_kwargs,
    )
```

For the QUIC branch (`cli.py:69-83`), resolve before launching the server:

```python
    if args.transport == "quic":
        try:
            from zakuro.worker.quic_server import DEFAULT_PORT, run_quic_worker
        except ImportError as exc:
            sys.exit(
                "`--transport quic` requires the `[worker]` extra (aioquic), which "
                f"is not installed: {exc}.\n{_WORKER_EXTRA_HINT}"
            )
        port = (
            args.port
            if args.port is not None
            else int(os.environ.get("ZAKURO_PORT", str(DEFAULT_PORT)))
        )
        from zakuro.worker.posture import (
            InsecureBindError,
            StartupConfigError,
            resolve_listener,
        )

        try:
            host = resolve_listener(args.host, port)
        except (InsecureBindError, StartupConfigError) as exc:
            sys.exit(str(exc))
        run_quic_worker(host=host, port=port)
        return
```

- [ ] **Step 6: Wire into the secondary `server.py:main` and `quic_server.py:__main__`**

In `zakuro/worker/server.py` `main()` (`:401-415`), after `args = parser.parse_args()` and the worker-name handling, add before `uvicorn.run`:

```python
    from zakuro.worker.posture import (
        InsecureBindError,
        StartupConfigError,
        resolve_listener,
    )

    try:
        host = resolve_listener(args.host, args.port)
    except (InsecureBindError, StartupConfigError) as exc:
        raise SystemExit(str(exc)) from exc

    uvicorn.run(
        "zakuro.worker.server:app",
        host=host,
        port=args.port,
        reload=False,
    )
```

In `zakuro/worker/quic_server.py`, the `if __name__ == "__main__":` parser path (`:365-382`) calls `run_quic_worker(host=args.host, port=args.port)`. Replace that final call with:

```python
    from zakuro.worker.posture import (
        InsecureBindError,
        StartupConfigError,
        resolve_listener,
    )

    try:
        host = resolve_listener(args.host, args.port)
    except (InsecureBindError, StartupConfigError) as exc:
        raise SystemExit(str(exc)) from exc
    run_quic_worker(host=host, port=args.port)
```

Leave the `run_quic_worker(...)` **function** signature/default (`host: str = "0.0.0.0"`) unchanged so existing library callers and `tests/test_quic_worker.py` are unaffected — the policy is enforced at the process entry boundary only.

- [ ] **Step 7: Keep the dev mesh working under the new policy**

Dev compose files start workers on `0.0.0.0` in legacy/no-auth mode, so they would now be refused. Add the explicit, eyes-open override to each worker service. In `docker/docker-compose.yml`, `docker/docker-compose.mesh.yml`, `docker/docker-compose.two-nodes.yml` (and any other compose file with a worker service), add to the worker service `environment:` block:

```yaml
      # Dev mesh is network-isolated (tailnet); accept the unauthenticated
      # /execute surface explicitly. Production sets ZAKURO_WIRE=v1 + a key
      # or ZAKURO_AUTH_REQUIRED=1 instead.
      ZAKURO_INSECURE_BIND: "1"
```

- [ ] **Step 8: Run the full worker test suite to confirm no regressions**

Run: `python -m pytest tests/test_posture.py tests/test_quic_worker.py tests/test_worker_runner.py tests/auth -v`
Expected: PASS (new posture tests + unchanged worker/auth tests).

- [ ] **Step 9: Commit**

```bash
git add zakuro/worker/posture.py zakuro/worker/cli.py zakuro/worker/server.py \
        zakuro/worker/quic_server.py tests/test_posture.py docker/docker-compose*.yml
git commit -m "feat(worker): refuse insecure non-loopback bind (fail-closed RCE guard)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Inbound payload size cap at the `unwrap_payload` chokepoint

**Files:**
- Modify: `zakuro/worker/envelope.py:86-101` (`unwrap_payload`)
- Test: `tests/test_envelope_size_cap.py`

**Interfaces:**
- Consumes: `zakuro.worker.posture.max_payload_bytes` (Task 1); `EnvelopeRejectedError` (existing).
- Produces: size-capped behaviour in `unwrap_payload` for **both** legacy and strict modes (shared by HTTP `server.py:354` and QUIC `quic_server.py:84`).

Rationale: a single oversized body can force a large allocation / cloudpickle before any other check. Cap the raw body once, at the shared chokepoint, in front of the strict/legacy branch.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_envelope_size_cap.py
import pytest

from zakuro.worker.envelope import EnvelopeRejectedError, unwrap_payload


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("ZAKURO_WIRE", "ZAKURO_MAX_PAYLOAD_BYTES"):
        monkeypatch.delenv(k, raising=False)


def test_oversize_legacy_body_rejected(monkeypatch):
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")
    with pytest.raises(EnvelopeRejectedError):
        unwrap_payload(b"x" * 17)


def test_undersize_legacy_body_passes(monkeypatch):
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")
    inner, env = unwrap_payload(b"x" * 10)
    assert inner == b"x" * 10
    assert env is None


def test_oversize_strict_body_rejected_before_decode(monkeypatch):
    monkeypatch.setenv("ZAKURO_WIRE", "v1")
    monkeypatch.setenv("ZAKURO_HMAC_KEY", "00" * 32)
    monkeypatch.setenv("ZAKURO_MAX_PAYLOAD_BYTES", "16")
    with pytest.raises(EnvelopeRejectedError):
        unwrap_payload(b"x" * 64)  # rejected on size, never reaches HMAC
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_envelope_size_cap.py -v`
Expected: FAIL — `test_oversize_legacy_body_rejected` does not raise (no cap today).

- [ ] **Step 3: Write minimal implementation**

Edit `unwrap_payload` in `zakuro/worker/envelope.py`, inserting the cap as the first thing the function does (before the `if not is_wire_strict():` branch):

```python
def unwrap_payload(raw: bytes) -> tuple[bytes, Envelope | None]:
    """Return ``(inner_payload, envelope_or_None)`` for the executor.

    ... (existing docstring unchanged) ...
    """
    from zakuro.worker.posture import StartupConfigError, max_payload_bytes

    try:
        cap = max_payload_bytes()
    except StartupConfigError:  # malformed cap at request time — fail closed
        cap = 256 * 1024 * 1024
    if len(raw) > cap:
        raise EnvelopeRejectedError(
            f"request body {len(raw)} bytes exceeds ZAKURO_MAX_PAYLOAD_BYTES ({cap})"
        )

    if not is_wire_strict():
        return raw, None
    # ... rest of the function unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_envelope_size_cap.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run existing envelope/wire tests to confirm no regression**

Run: `python -m pytest tests/wire -v`
Expected: PASS (existing envelope/wire suite, e.g. `test_envelope_wiring.py`, unaffected — default cap is 256 MiB).

- [ ] **Step 6: Commit**

```bash
git add zakuro/worker/envelope.py tests/test_envelope_size_cap.py
git commit -m "feat(worker): cap inbound /execute body size at unwrap chokepoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Route `middleware._auth_required` through the posture predicate (DRY)

**Files:**
- Modify: `zakuro/auth/middleware.py:58-59`
- Test: `tests/auth/test_middleware.py` (extend with one parity test)

**Interfaces:**
- Consumes: `zakuro.worker.posture.is_auth_required` (Task 1).
- Produces: unchanged `_auth_required()` behaviour, now delegating (single source of truth). `posture` does not import FastAPI, so no import cycle.

- [ ] **Step 1: Write the failing test**

```python
# tests/auth/test_middleware.py  (append)
def test_auth_required_delegates_to_posture(monkeypatch):
    from zakuro.auth import middleware
    from zakuro.worker import posture

    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "1")
    assert middleware._auth_required() is posture.is_auth_required() is True
    monkeypatch.setenv("ZAKURO_AUTH_REQUIRED", "0")
    assert middleware._auth_required() is posture.is_auth_required() is False
```

- [ ] **Step 2: Run test to verify it fails (or is brittle) against the duplicated impl**

Run: `python -m pytest tests/auth/test_middleware.py::test_auth_required_delegates_to_posture -v`
Expected: With the current duplicated literal in `middleware`, the `is` identity against `posture.is_auth_required()` may not hold for casing differences; this test pins the delegation. If it passes incidentally, proceed — the refactor still removes the duplication.

- [ ] **Step 3: Write minimal implementation**

Replace `_auth_required` in `zakuro/auth/middleware.py`:

```python
def _auth_required() -> bool:
    from zakuro.worker.posture import is_auth_required

    return is_auth_required()
```

- [ ] **Step 4: Run the auth suite to verify it passes**

Run: `python -m pytest tests/auth -v`
Expected: PASS (existing `test_middleware.py` + new parity test).

- [ ] **Step 5: Commit**

```bash
git add zakuro/auth/middleware.py tests/auth/test_middleware.py
git commit -m "refactor(auth): delegate _auth_required to posture (single source of truth)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Documentation — SECURITY.md, env-var reference, deferred-flip note

**Files:**
- Modify: `SECURITY.md` (repo root)
- Modify: `docs/PROTOCOL.md` (note the size cap + bind policy)
- Create: `CHANGELOG.md` entry **only if the file exists** in this checkout (it is absent in the public mirror; skip if missing)

**Interfaces:** none (docs only).

- [ ] **Step 1: Read the current SECURITY.md to find the insertion point**

Run: `sed -n '1,80p' SECURITY.md`
Expected: locate a "Hardening", "Deployment", or "Reporting" section to append after.

- [ ] **Step 2: Append the worker-exposure hardening section to `SECURITY.md`**

Add this block (after the existing hardening/deployment content; if no such section exists, add it as a new top-level `##` section):

```markdown
## Worker network exposure (fail-closed bind)

A Zakuro worker's `/execute` endpoint deserialises cloudpickle. Treat any
worker reachable off a trusted, network-isolated mesh as a remote-code-execution
surface unless a caller-authentication control is enabled. From v0.2.24 the
worker **refuses to bind a non-loopback address** when none of these is on:

| Control | Enable with |
| --- | --- |
| JWT auth (RFC 0002) | `ZAKURO_AUTH_REQUIRED=1` |
| HMAC-signed wire (RFC 0001) | `ZAKURO_WIRE=v1` + `ZAKURO_HMAC_KEY` or `ZAKURO_HMAC_KEY_FILE` |
| Mutual TLS | `ZAKURO_CERT_DIR=<dir>` |

Escape hatch for a trusted, isolated mesh: `ZAKURO_INSECURE_BIND=1` (logs a
startup WARNING). Loopback binds (`--host 127.0.0.1`) are always allowed.

Other hardening env vars:

- `ZAKURO_MAX_PAYLOAD_BYTES` — inbound `/execute` body cap (default 256 MiB).

> **Roadmap (v0.4, cross-repo):** `ZAKURO_WIRE=v1` and `ZAKURO_AUTH_REQUIRED=1`
> will become the *defaults* once the SDK client wraps signed envelopes and the
> `zc` broker rollout is coordinated. Until then they are opt-in and this
> fail-closed bind guard is the safety net.
```

- [ ] **Step 3: Add a short note to `docs/PROTOCOL.md`**

Append under the wire-format section:

```markdown
### Inbound size cap & bind policy

The worker rejects any `/execute` body larger than `ZAKURO_MAX_PAYLOAD_BYTES`
(default 256 MiB) at the `unwrap_payload` chokepoint, before decode/HMAC. It
also refuses to bind a non-loopback interface when no caller-authentication
control (`ZAKURO_AUTH_REQUIRED` / `ZAKURO_WIRE=v1` / `ZAKURO_CERT_DIR`) is
enabled, unless `ZAKURO_INSECURE_BIND=1` is set. See `SECURITY.md`.
```

- [ ] **Step 4: Add a CHANGELOG entry if the file exists**

Run: `test -f CHANGELOG.md && echo present || echo absent`
If `present`, add under the `## [Unreleased]` heading:

```markdown
### Added
- Worker fail-closed bind guard: refuses a non-loopback bind without a
  caller-authentication control unless `ZAKURO_INSECURE_BIND=1`.
- `ZAKURO_MAX_PAYLOAD_BYTES` inbound `/execute` size cap (default 256 MiB).
- Startup validation of `ZAKURO_WIRE` / `ZAKURO_AUTH_REQUIRED` (fail closed on typos).
```
If `absent`, skip (the internal repo tracks this; the public mirror has no CHANGELOG yet).

- [ ] **Step 5: Commit**

```bash
git add SECURITY.md docs/PROTOCOL.md CHANGELOG.md 2>/dev/null; git add SECURITY.md docs/PROTOCOL.md
git commit -m "docs: document worker fail-closed bind, size cap, and deferred v0.4 flip

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (run after all tasks)

- [ ] **Full test suite + coverage:** `python -m pytest tests/ -v` → all pass.
- [ ] **Lint/type:** `ruff check zakuro/ tests/ && mypy zakuro/` → clean.
- [ ] **Negative path (the bug, now fixed):**
  `ZAKURO_AUTH_REQUIRED= ZAKURO_WIRE= python -m zakuro.worker.cli --host 0.0.0.0`
  → exits non-zero with the "refusing to bind … unauthenticated remote-code-execution" message.
- [ ] **Loopback still works:** `python -m zakuro.worker.cli --host 127.0.0.1` → starts (INFO posture banner).
- [ ] **Override works:** `ZAKURO_INSECURE_BIND=1 python -m zakuro.worker.cli --host 0.0.0.0` → starts, logs a WARNING banner.
- [ ] **Secured works:** `ZAKURO_WIRE=v1 ZAKURO_HMAC_KEY=$(python -c "print('00'*32)") python -m zakuro.worker.cli --host 0.0.0.0` → starts.
- [ ] **Size cap:** post a >256 MiB body to a running loopback worker → `401`, worker logs the cap rejection.
- [ ] **Dev mesh:** `docker compose -f docker/docker-compose.mesh.yml up` → workers start (now carrying `ZAKURO_INSECURE_BIND=1`).

## Deferred / follow-up (NOT in this plan)

These are intentionally out of scope — they require cross-repo coordination or are tracked elsewhere:

1. **v0.4 default-flip:** make `ZAKURO_WIRE=v1` + `ZAKURO_AUTH_REQUIRED=1` the defaults. Blocked on: SDK client building signed envelopes (`zakuro.client.envelope`, "forthcoming") + JWT issuance on the client path + coordinated `zc` broker release sharing the wire format. Track as its own RFC/spec.
2. **Dependency upper bounds** (Wave 1) and **Docker demo/compute pinning + 3.14-builder/3.11-runtime ABI fix** (Wave 1).
3. **Coverage floor `--cov-fail-under`** (issue #22, Wave 2) and the **red "Deploy notebook (staging)" CI lane** (Wave 2).
4. **Public-mirror version sync** (v0.2.0 ↔ v0.2.23) and **`master`→`main`** (issue #25, Wave 3).
