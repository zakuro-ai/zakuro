# Final Fix Report — feat/worker-failclosed-security

## FIX A — [CRITICAL] server.py mTLS fail-open

**File:** `zakuro/worker/server.py` (`main()`, after `resolve_listener` block)

**What changed:** Added an `ssl_kwargs` block identical to `cli.py` that reads
`ZAKURO_CERT_DIR`, defers the `zakuro.transport` import, calls `load_server_tls()`,
and passes `ssl_certfile / ssl_keyfile / ssl_ca_certs / ssl_cert_reqs=2` to
`uvicorn.run(**ssl_kwargs)`. Without this, setting `ZAKURO_CERT_DIR` caused the
bind guard to permit a non-loopback bind but then uvicorn served plaintext — an
mTLS fail-open / unauthenticated RCE.

Also added the two explanatory comments that were missing from `cli.py`'s SSL block:
- `# Defer the import: zakuro.transport pulls in cryptography.`
- `# uvicorn accepts ssl.CERT_REQUIRED as int (2) -> require client certs.`

**New tests** added to `tests/transport/test_wiring.py`:
- `test_server_passes_ssl_kwargs_when_cert_dir_set` — CERT_DIR set → all four ssl_* kwargs captured in uvicorn.run mock.
- `test_server_no_ssl_kwargs_when_cert_dir_unset` — CERT_DIR unset, loopback bind → no ssl_* kwargs.

---

## FIX B — [IMPORTANT] compose.yaml quickstart crash-loop

**File:** `compose.yaml` — both `zakuro-worker` and `zakuro-worker-dev` services

**What changed:** Both services had `ZAKURO_HOST=0.0.0.0` with no security control,
causing crash-loops after the fail-closed guard landed. Added to each service's
`environment:` list:
```yaml
      # LOCAL quickstart only: ...
      - ZAKURO_INSECURE_BIND=1
```

---

## FIX C — [MINOR/DRY] envelope.py size-cap fallback hardcoded default

**File:** `zakuro/worker/envelope.py` line 100 (inside `unwrap_payload`)

**What changed:** Import line changed from
`from zakuro.worker.posture import StartupConfigError, max_payload_bytes`
to
`from zakuro.worker.posture import _DEFAULT_MAX_PAYLOAD, StartupConfigError, max_payload_bytes`
(ruff auto-sorted to alphabetical order with `_DEFAULT_MAX_PAYLOAD` first).

Fallback `cap = 256 * 1024 * 1024` replaced with `cap = _DEFAULT_MAX_PAYLOAD`.

---

## FIX D — [MINOR] posture.py falsey token vocab

**File:** `zakuro/worker/posture.py` line 26

**What changed:**
```python
_FALSE = {"", "0", "false", "no"}
# →
_FALSE = {"", "0", "false", "no", "off", "none"}
```
`ZAKURO_AUTH_REQUIRED=off` and `=none` no longer raise `StartupConfigError`.
Auth-enabled detection (`is_auth_required`) unchanged — it checks `_TRUE` only.

---

## FIX E — [MINOR] misleading test name

**File:** `tests/test_posture.py` line 107

**What changed:** `test_loopback_bind_allowed_when_insecure` → `test_loopback_always_allowed`.
Body unchanged. Loopback is unconditionally allowed; the old name implied it needed
an insecure env flag, which is wrong.

---

## Verification Outputs

### Full suite
```
354 passed, 6 warnings in 17.01s
```
(352 prior + 2 new server ssl tests)

### Targeted transport + posture tests
```
28 passed, 4 warnings in 0.73s
```

### Ruff
```
All checks passed!
```

### mypy
server.py, envelope.py, posture.py — 61 errors in 15 files.
The `**ssl_kwargs` arg-type errors on server.py line 445 (4 new) match the
pre-existing pattern already present in cli.py (22 errors on line 144).
No new categories of error introduced.

### Sanity — insecure bind still refused
```
env -u ZAKURO_AUTH_REQUIRED -u ZAKURO_WIRE -u ZAKURO_INSECURE_BIND -u ZAKURO_CERT_DIR \
  uv run python -m zakuro.worker.server --host 0.0.0.0 2>&1 | grep -c "refusing to bind"
1
```
