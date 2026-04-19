# `zc` QUIC worker caller — draft PR

This directory holds the draft of the patch that teaches the `zc` broker to call workers over QUIC instead of HTTP. It mirrors the wire format in [`docs/PROTOCOL.md`](../PROTOCOL.md) and slots in next to the existing HTTP `forward_to_worker` in `src/broker/server.rs`.

Status: **not yet applied upstream.** Everything here is a text artefact designed to be `git apply`-able after review.

## Layout

```
docs/zc-quic-patch/
├── README.md              ← this file
├── Cargo.toml.patch       ← pyproject-equivalent deps bump (quinn et al. already present)
├── src/broker/worker_quic.rs  ← new module, drop in under src/broker/
└── src/broker/server.rs.patch ← unified diff adding a QUIC code path to handle_execute
```

## Design recap

- **Wire protocol** — `docs/PROTOCOL.md` in this repo. ALPN `"zk-worker"`, bidi streams, 1-byte op + 4-byte BE length + payload. Response: 1-byte status + 4-byte BE length + payload.
- **Connection pool** — one `quinn::Connection` cached per `host:port` inside the broker process. Workers are long-lived, so we keep the connection open across many requests. No handshake per call, big RPS win.
- **Integration point** — inside `handle_execute` the broker already picks a `Worker` with a `uri` string. If the URI scheme is `quic://` we call `forward_to_worker_quic`; else `forward_to_worker` (the existing HTTP fn). Nothing else changes in the routing / billing / WAL paths, because the payload bytes and response bytes are identical to HTTP.
- **Tokio runtime** — reuse the runtime already spawned in `src/broker/quic.rs` for the broker-mesh QUIC server; don't create a second one.

## Applying

```bash
# From a clean clone of zakuro-ai/zc
cd zc
cp ../zakuro/docs/zc-quic-patch/src/broker/worker_quic.rs src/broker/
patch -p0 < ../zakuro/docs/zc-quic-patch/Cargo.toml.patch
patch -p0 < ../zakuro/docs/zc-quic-patch/src/broker/server.rs.patch
cargo build --release
```

Then register the module in `src/broker/mod.rs` alongside the existing ones:

```rust
pub mod worker_quic;
```

## Rollout

1. Ship the patch with **no** behaviour change: `quic://` URIs have never existed in `zc`'s worker registry, so the code path is inert.
2. Update the worker discovery logic in `src/broker/discovery.rs` to probe both HTTP `/health` and QUIC HEALTH, preferring QUIC when both answer.
3. Once at least one worker in each mesh is QUIC-capable, flip the default route selection to prefer `quic://` URIs.
4. Deprecate the HTTP hot path when the population has migrated.

## Testing

Run the Python reference implementation:

```bash
# In the zakuro repo
uv run zakuro-worker --transport quic --port 4433
```

Then from zc's integration test suite (pseudo):

```rust
let body = cloudpickle::dump(&Request { func, args, kwargs });
let resp = worker_quic::forward("quic://127.0.0.1:4433", &body, "req-1", 30.0).await?;
let result: MyReturn = cloudpickle::load(&resp)?;
```

The zakuro Python test suite (`tests/test_quic_worker.py`) already validates the server side byte-for-byte.
