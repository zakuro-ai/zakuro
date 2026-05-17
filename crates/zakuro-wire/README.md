# zakuro-wire

[![crates.io](https://img.shields.io/crates/v/zakuro-wire.svg)](https://crates.io/crates/zakuro-wire)
[![docs.rs](https://img.shields.io/docsrs/zakuro-wire)](https://docs.rs/zakuro-wire)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)

Shared **postcard** wire format for the [Zakuro](https://github.com/zakuro-ai/zakuro) distributed-ML runtime.

This crate is the single source of truth for the byte-level shape of every dispatch payload that travels between:

- the **Zakuro worker** (Python, [`zakuro-ai/zakuro`](https://github.com/zakuro-ai/zakuro)) — runs `@zk.fn`-decorated callables;
- the **zc broker** (Rust, [`zakuro-ai/zc`](https://github.com/zakuro-ai/zc)) — routes dispatches and meters credits.

Putting the schema in its own crate, rather than duplicating struct definitions in each repo, makes the contract mechanically enforceable: both consumers depend on the same crate version via Cargo's resolver, and any wire-incompatible change forces a coordinated version bump.

See [zakuro RFC 0001](https://github.com/zakuro-ai/zakuro/blob/master/docs/rfcs/0001-wire-format-postcard.md) for the architectural decision.

## v0.1 surface

| Type | Direction | Notes |
|---|---|---|
| `Envelope` | client → broker → worker | callable + args + HMAC + resource budget |
| `ExecutionResult` | worker → broker → client | status / exit_code / stdout / stderr / structured error |
| `WireVersion` | both | top-level version tag carried by every payload |
| `Status`, `ErrorKind` | both | structured outcome variants |
| `ResourceLimits` | both | cpus / memory_mb / gpus / timeout_seconds |

## Wire compatibility

`postcard` serialisation is **append-only within a major version**. The crate guarantees:

- New fields land as `#[serde(default)]`; old peers accept payloads with trailing unknown fields.
- Enum variants are appended, never reordered.
- A truly breaking change (renamed field, repurposed variant) requires a major version bump on this crate **and** an ALPN string bump on the QUIC transport so peers refuse to talk past each other.

A `tests/snapshots.rs` test freezes the byte representation of a canonical envelope; any schema change that breaks compatibility flips this red before publication.

## Quick start

```rust
use zakuro_wire::{Envelope, ResourceLimits, WireVersion};

let env = Envelope {
    version: WireVersion::V1,
    job_id: "j-1".into(),
    tenant_id: "tenant-acme".into(),
    callable: vec![/* cloudpickle bytes, HMAC-signed */],
    args: vec![],
    hmac: [0u8; 32],
    resource_limits: ResourceLimits {
        cpus: 1.0,
        memory_mb: 1024,
        gpus: 0,
        timeout_seconds: 60,
    },
};

let bytes = postcard::to_allocvec(&env)?;
// ... send `bytes` to the worker over QUIC ...
let back: Envelope = postcard::from_bytes(&bytes)?;
assert_eq!(env, back);
```

## Out of scope

- The **HMAC verification path** itself. This crate carries the HMAC bytes; verification (per-tenant key derivation, signature check) is in `zakuro.wire.safe_loads` on the Python side and the equivalent Rust adapter in `zc`. Keeping the schema crate verification-free avoids tying it to a specific crypto implementation.
- **Gossip protocol messages** (RFC 0008). They live in the broker for v0.1; a v0.2 of this crate adds them once the broker-side implementation is ready to consume them from a shared definition.

## License

BSD-3-Clause. See [LICENSE](LICENSE).
