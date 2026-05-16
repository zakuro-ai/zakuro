# RFC 0001 — Wire format: replace cloudpickle with postcard

- **Status:** Accepted (2026-05)
- **Closes:** [#117](https://github.com/zakuro-ai/zakuro/issues/117)
- **Authors:** Maintainers + AI cleanup-sprint pass

## Context

Today `zakuro.worker.server` and `zakuro.client.ZakuroClient` ship Python callables across the network using `cloudpickle.dumps` / `cloudpickle.loads`. This is:

1. **An RCE primitive.** `cloudpickle.loads` on an attacker-influenced byte stream executes arbitrary code in the worker's process. The plugin POC's [`THREAT_MODEL.md`](https://github.com/zakuro-ai/sakura/blob/master/THREAT_MODEL.md) calls this out as the highest single-point-of-risk in the runtime.
2. **The blocker on the Semgrep ratchet.** The `zakuro.deserialization.cloudpickle-loads-untrusted` rule (`.semgrep/zakuro.yml`) fires on every `cloudpickle.loads` call site. The SAST lane is `continue-on-error: true` *because* this rule cannot be honoured today. Removing the call sites flips the lane back to strict.
3. **Cross-language brittle.** A Rust broker (`zc`) cannot inspect a cloudpickle payload without bundling a CPython interpreter. The postcard pivot is what unlocks the broker doing routing decisions on payload metadata.

## Decision

**Adopt `postcard` (Rust + Python) as the wire format for `ExecutionPlan`, `ExecutionResult`, and the function-dispatch envelope. Restrict `cloudpickle` to a narrow, audited path used only for the *callable body* of an `@zk.fn` and only across mTLS-verified trusted peers.**

Specifically:

- The **envelope** (job-id, tenant-id, resource limits, hash of the callable, hash of the args) is postcard-encoded `serde` structs defined in `crates/zakuro-wire`.
- The **callable body** (function bytecode + closure cells) and the **args** are carried as opaque `bytes` fields inside the postcard envelope.
- The body is signed (HMAC with a per-tenant key derived from the JWT — see [RFC 0002](0002-auth-mtls-jwt.md)) and the signature is verified before any `cloudpickle.loads` call.
- A worker that receives an envelope whose signature does not verify drops the payload and emits a security event without invoking the deserialiser.

Rationale for postcard:

- Already in `sakura`'s workspace deps (`postcard = "1.0"`) so the broker can decode without a new external dep.
- Zero-copy on the Rust side; ~2× faster than msgpack for our payload shape.
- `serde`-driven on both ends — a Python binding via PyO3 + `serde-pyobject` gives ergonomic conversion to/from Pydantic models with no schema duplication.
- Forward-compatibility via `#[serde(default)]` and the new `Variant: Future` enum tail — same migration tools as JSON.

## Implementation plan

**Step 0 — schema crate** (`crates/zakuro-wire`):

- Define `pub struct Envelope`, `pub struct Job`, `pub struct Result`, `pub enum Error` with `#[derive(Serialize, Deserialize)]`.
- Pin postcard to the workspace version. Re-export `postcard::to_allocvec` / `postcard::from_bytes` as the only API the binding crate calls.
- Snapshot tests against frozen byte vectors so future schema changes are explicit.

**Step 1 — Python binding** (`zakuro/_wire/`):

- New tiny extension via PyO3 + `serde-pyobject` exposing `pack(envelope: dict) -> bytes` and `unpack(blob: bytes) -> dict`.
- Wrap into `zakuro.wire.safe_loads(blob: bytes, *, public_key) -> Envelope` that verifies the HMAC and refuses any unauthenticated input.
- Mark `zakuro.wire.unsafe_pickle_load` (the cloudpickle fallback) with a deprecation warning and a `# type: ignore[zakuro-safe-pickle]` escape hatch the Semgrep rule already understands.

**Step 2 — replace call sites**:

- `zakuro/worker/server.py` `/execute` handler — was `cloudpickle.loads(payload)`. Becomes:

  ```python
  envelope = zakuro.wire.safe_loads(payload, public_key=tenant_key(request))
  callable_blob = envelope.callable
  # Inside the verified envelope, cloudpickle.loads is the trusted path.
  func = cloudpickle.loads(callable_blob)
  ```

- `zakuro/client.py` `execute()` — wrap the cloudpickled callable in `Envelope { callable: bytes, args: bytes, ... }` and serialise with `zakuro.wire.pack(...)` before sending.
- `sakura/dispatch/remote.py:27` (the one Semgrep flags) — same shape on the sakura side.

**Step 3 — flip Semgrep to strict**:

- Drop the `--error`-disabled run in `.github/workflows/sast.yml`.
- Update `.semgrep/zakuro.yml` to allow `cloudpickle.loads` only inside `zakuro.wire.*` and `tests/`.

**Step 4 — wire-protocol version bump**:

- Bump the QUIC ALPN to `zk-worker-v2` so a v1 client cannot accidentally talk to a v2 worker (the v1 payload is raw cloudpickle; v2 is a postcard envelope).
- Document in [`docs/PROTOCOL.md`](../PROTOCOL.md) — v2 envelope shape + HMAC verification flow.

## Rejected alternatives

| Option | Why rejected |
|---|---|
| msgpack | Schema-less, but slower than postcard on our payload shape and adds a dep on both sides. Postcard already in workspace. |
| protobuf | Schema-enforced and mature, but the build pipeline cost (proto compilation, generated code in two languages, breaking on schema changes) is high for the marginal benefit. |
| Pydantic-only JSON | Already in deps and human-debuggable, but ~10× slower than postcard at the payload sizes we measure on the bench harness — not acceptable for high-RPS broker dispatch. |
| Keep cloudpickle, add mTLS-only peer auth | Closes the network-attacker case but leaves the in-process / sidecar attacker (someone who can talk to a worker over a Unix socket) with full RCE. Defence-in-depth requires payload-level signing too. |

## Migration / rollout

1. RFC merged (this PR).
2. `crates/zakuro-wire` lands (Step 0). No runtime callers yet.
3. Python binding lands (Step 1). Re-export wired into `zakuro.wire`; the existing cloudpickle path stays alongside under a feature flag.
4. Worker + client switch over (Step 2). End-to-end bench harness checked: round-trip latency stays within −5 % / +5 % of cloudpickle.
5. Semgrep ratcheted (Step 3).
6. ALPN bump (Step 4) under a deprecation window — v1 supported for two minor releases, removed in the third.

## Open questions for implementation time

- HMAC key derivation: per-tenant or per-worker? Decision deferred to RFC 0002.
- `serde-pyobject` vs a hand-rolled `From<PyAny>` impl: defer to first implementation PR; benchmark both.
- Compression (zstd?) on the envelope's `args` field for ML payloads: defer to a separate "wire payload size" investigation once v2 is live.
