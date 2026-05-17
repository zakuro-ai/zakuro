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

## Amendment — v0.2 wire (#174, #175)

**Status:** Accepted (2026-05). Crate version bumped to `zakuro-wire = 0.2.0`.

v0.2 introduces three new types alongside (not replacing) the v0.1 `Envelope`. v0.1 callers stay byte-compatible; v0.2-capable workers accept both.

### Added types

- `EnvelopeV2` — same shape as `Envelope` plus:
  - `cache_key: Option<String>` — when present, the worker stores the (reconstructed) callable bytes in its bounded LRU under this key.
  - `delta_against: Option<String>` — when present, `callable` is a delta against the value cached under this key. The worker reconstructs the full payload before invoking the deserialiser. Cache-miss is signalled as `ErrorKind::WorkerUnavailable { reason: "cache_miss" }`; the caller retries with the full payload.
- `ChunkFrame` — one frame in a multi-chunk streaming dispatch:
  - `stream_id: u64` — distinguishes concurrent chunked dispatches.
  - `seq: u32` — 0-indexed monotonic sequence within the stream.
  - `last: bool` — true on the final chunk; triggers reassembly.
  - `bytes: Vec<u8>` — chunk payload. Concatenation in `seq` order yields a postcard-encoded `EnvelopeV2`.
- `V2Message` — top-level enum: `Envelope(EnvelopeV2)` or `Chunk(ChunkFrame)`. Callers serialise this so the worker dispatches on the variant tag.

### Wire-format invariants

- `Envelope` v0.1 byte layout is **unchanged**. A regression test (`v1_envelope_first_byte_unchanged`) asserts the first byte is still `0x00`.
- `EnvelopeV2` starts with `0x01` (`WireVersion::V2`). v0.1 workers reject this as `UnknownWireVersion`.
- `Option<String>` follows postcard convention: `0x00` = None; `0x01` + length-prefixed UTF-8 when Some. A None-valued v0.2 envelope is byte-shorter than a Some-valued one (regression test asserts this so an absent-deltas dispatch isn't unfairly penalised).

### Backwards / forwards compat

- A v0.1 broker dispatching v0.1 `Envelope`s to a v0.2 worker → works unchanged.
- A v0.2 broker dispatching `EnvelopeV2` to a v0.1 worker → worker rejects with `UnknownWireVersion` (the first-byte tag is `0x01`, which doesn't exist in v0.1's `WireVersion` enum). Broker falls back to v0.1 `Envelope` per the worker's `/info`-advertised wire-version (forthcoming claim on `/info`).
- A v0.2 broker dispatching `EnvelopeV2` with `delta_against: Some(...)` to a v0.2 worker whose cache is cold → worker replies `WorkerUnavailable { reason: "cache_miss" }`; broker retries with full payload (no delta).

### Cache eviction policy

Cache lives on the worker, bounded by an LRU of N entries × cap-per-entry, both configurable via `ZAKURO_CACHE_MAX_ENTRIES` / `ZAKURO_CACHE_MAX_BYTES`. Defaults: 32 entries / 4 GB. Eviction is silent — callers that suffer a cache-miss on a delta retry with the full payload, which is the same code path as never having cached.

Implementation: [`zakuro.wire.cache.PayloadCache`](https://github.com/zakuro-ai/zakuro/blob/master/zakuro/wire/cache.py) (an ordered-dict LRU; promotes on `get`; evicts oldest entry on either cap violation; refuses to admit a single value larger than the bytes budget).

### Delta-apply algorithm

The wire crate carries `callable` bytes opaquely. When `delta_against = Some(prev_key)`, the worker reconstructs:

```python
full = bsdiff4.patch(cache[prev_key], envelope.callable)
```

**Format choice: bsdiff4.** Reasoning:

- It's well-established (the format `bsdiff`+`bspatch` use, in production since 2003).
- `bsdiff4` ships as a pure-Python lib with a tiny C accelerator; <50 KB wheel.
- It produces meaningfully smaller deltas than `zstd --train` for binary state-dicts where most bytes carry small numeric changes (the common case in fine-tuning).
- It's *not* tensor-aware — a future tensor-aware diff (e.g. quantised-residual or low-rank delta) could supplant it. When that happens we add a `delta_format: Option<DeltaFormat>` field to EnvelopeV2 and gate selection on it. For v0.2 we hard-code bsdiff4.

The delta isn't authenticated by itself: the HMAC over `(callable, args, job_id)` in the envelope covers the *delta bytes* as they arrive. A reconstructed payload that doesn't match the broker's intent produces a different HMAC than the broker computed, so `safe_loads` rejects with `HmacMismatchError` before the bytes reach cloudpickle. The cache layer is not the security boundary.

### Phase 2 substrate (current) vs Phase 2 wiring (next)

The substrate ships in two slices:

1. **Substrate (this PR / #174 Phase 2 substrate):** `zakuro/wire/cache.py` with `PayloadCache` LRU + `apply_delta` (bsdiff4). 15 unit tests. Module is standalone; no worker integration yet.
2. **Wiring (next PR, after #200 lands):** the QUIC handler's `_handle_chunk` calls `cache.get(env.delta_against)` + `apply_delta` before forwarding to the executor; `cache.put(env.cache_key, callable_bytes)` after successful dispatch. Cache miss / DeltaApplyError surface as `WorkerUnavailable { reason: "cache_miss" }` so the broker retries with the full payload.

This split mirrors the #115 / #116 / #117 substrate-vs-wiring pattern: the substrate has its own narrow tests; the wiring exercises the end-to-end flow.

### Cross-repo

`zc` adopts `zakuro-wire = 0.2.0` simultaneously per the multi-repo coherence rule. The integration test in `.github/workflows/integration.yml` covers the matrix: v0.1 broker × v0.1 worker (today), v0.1 broker × v0.2 worker (back-compat), and v0.2 broker × v0.2 worker (new path) once the matrix expands.
