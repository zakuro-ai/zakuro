# Zakuro worker wire protocol — QUIC

This is the spec for the binary QUIC protocol used between a caller (Python client, `zc` broker, or another worker) and a Zakuro worker. It replaces the HTTP-based `/execute` endpoint in latency-sensitive paths while preserving identical cloudpickle semantics on the payload.

Reference implementations live in this repo:

- Server: `zakuro/worker/quic_server.py` (`aioquic`)
- Client: `zakuro/processors/quic.py` (`aioquic`)

This spec is authoritative — language bindings (Rust, Go, …) should match it verbatim.

## 1. Transport

- **UDP / QUIC v1** (RFC 9000 + TLS 1.3 per RFC 9001).
- **Default port**: 4433.
- **ALPN protocol identifier**: `"zk-worker"` (ASCII, no length byte in this document — ALPN itself encodes length).
  - Distinct from `"zk-quic"` which zc's broker-mesh uses. A single UDP socket must not accept both ALPNs — brokers and workers should bind separate ports.
- **TLS**: self-signed by default. Clients skip CA verification but pin the peer via out-of-band knowledge (URI + mesh). The certificate is persisted to `$HOME/.zakuro/quic_worker_cert.der` + `quic_worker_key.der` on the server and regenerated if missing.

## 2. Frame format

All frames use **big-endian** integers and are exchanged on QUIC **bidirectional streams**. One request/response pair occupies exactly one stream — the client opens a new stream per call.

```
Request frame (client → server):
  +------+-------------+---------------------+
  | op:1 | length:4 BE | payload: length B   |
  +------+-------------+---------------------+

Response frame (server → client):
  +--------+-------------+---------------------+
  | stat:1 | length:4 BE | payload: length B   |
  +--------+-------------+---------------------+
```

- `length` is the exact byte count of `payload` and MUST be less than 2^32 − 1.
- After writing its single frame the client MUST close the send side of the stream (`finish`). After the server writes its response frame it MUST close the send side of the stream. Re-using a stream for multiple calls is not allowed.

### 2.1 Opcodes

| op | name          | request payload                                | response payload (stat=0)                |
|----|---------------|------------------------------------------------|------------------------------------------|
| 1  | EXECUTE       | cloudpickle of `{"func", "args", "kwargs"}`   | cloudpickle of `result` (may be `Exception`) |
| 2  | INFO          | empty (`length = 0`)                           | UTF-8 JSON — see §4                      |
| 3  | HEALTH        | empty (`length = 0`)                           | UTF-8 JSON `{"status":"ok"}`             |
| 4  | EXECUTE_CHUNK | postcard `ChunkFrame` — one chunk of a v0.2 multi-chunk dispatch (RFC 0001 amendment). Multiple chunks share a `ChunkFrame.stream_id`; final chunk has `last=true`. Concatenated bytes are a v0.2 `EnvelopeV2`. | non-final chunk: empty (`stat = 3` ack). Final chunk: cloudpickle of `result` (per EXECUTE). |

Unknown opcodes MUST produce `stat = 2` (see below) with a UTF-8 payload describing the error.

### 2.2 Status codes

| stat | meaning                                                                |
|------|------------------------------------------------------------------------|
| 0    | OK. `payload` is the successful response per the opcode.               |
| 1    | User error — the function ran and raised. `payload` is the cloudpickled `Exception` for EXECUTE; for other opcodes this status MUST NOT be used. |
| 2    | Protocol / server error — malformed frame, unknown opcode, worker overload. `payload` is UTF-8 text. |
| 3    | Chunk accepted, more chunks expected. Used only by `EXECUTE_CHUNK` (op=4) for non-final chunks. `payload` is empty. The caller continues sending chunks on new QUIC streams sharing the same `ChunkFrame.stream_id`. |

Rationale: separating `stat=1` (a successful RPC that returned a user exception) from `stat=2` (transport/protocol failure) lets the caller distinguish "function on worker raised" from "worker itself is broken," without requiring the caller to cloudpickle-probe every response.

## 3. EXECUTE payload

```python
cloudpickle.dumps({
    "func":   <callable>,    # any cloudpickle-serialisable callable
    "args":   <tuple>,
    "kwargs": <dict>,
})
```

The server deserialises, invokes `func(*args, **kwargs)` in its thread pool, then:

- on success, responds `stat=0` + `cloudpickle.dumps(result)`;
- on user exception, responds `stat=1` + `cloudpickle.dumps(exc)` — **not** `stat=2`. The caller is responsible for re-raising.

No timeout is enforced at the protocol layer — long-running calls keep the stream open. Callers should cancel the stream (QUIC `STOP_SENDING` + `RESET_STREAM`) to abort.

## 4. INFO payload

`stat=0` response is UTF-8 JSON matching the HTTP `/info` endpoint. Minimum required fields:

```json
{
  "name": "worker-B",
  "version": "0.2.x",
  "resources": {
    "cpus_total": 10.0,
    "cpus_available": 10.0,
    "memory_total": 17179869184,
    "memory_available": 9123840000,
    "gpus_total": 0,
    "gpus_available": 0
  }
}
```

Additional fields (`hardware`, `pricing`, `tags`, `worker_type`) are optional and SHOULD match the HTTP server's shape so broker routing logic can be reused unchanged.

## 5. Concurrency & flow control

- The server MUST accept many concurrent bidirectional streams per connection. Default cap: `max_concurrent_bidi_streams = 1024`.
- The server SHOULD set `max_stream_data` high enough to admit a full EXECUTE payload without extra flow-control round trips (1 MiB is a reasonable default; raise for large payloads).
- Idle timeout: 30 s recommended. Keep-alive via QUIC PING is optional.

## 6. TLS details

### 6.1 Certificate

- Self-signed, single SAN `localhost`. Persisted to `$HOME/.zakuro/quic_worker_cert.der` / `quic_worker_key.der`.
- Generated on first startup if absent (`rcgen::generate_simple_self_signed` in Rust, `cryptography` + `aioquic` helpers in Python).
- Rotated only when the files are deleted manually.

### 6.2 ALPN

Both endpoints MUST offer exactly `["zk-worker"]`. Handshake MUST fail if the peer does not advertise the same protocol — this prevents accidental cross-talk with `zk-quic` broker-mesh endpoints.

### 6.3 Peer authentication

At this layer there is no mTLS. Identity is established out-of-band (WireGuard, VPC). Callers requiring authentication SHOULD add an auth token to the EXECUTE payload (e.g. `{"auth": "...", "func": ..., ...}`) and have the server reject unauthenticated calls with `stat=2`.

## 7. Version negotiation

There is no explicit version field in v1. If future frame layouts change, define a new ALPN (`"zk-worker-v2"`). Clients advertise both, servers pick the highest mutually supported.

## 8. Compatibility with HTTP `/execute`

The cloudpickle payload for EXECUTE is **byte-identical** to the body the existing HTTP `/execute` endpoint accepts. Brokers and clients that already serialise with `cloudpickle.dumps({"func": ..., "args": ..., "kwargs": ...})` can retarget QUIC by:

1. Prepending `0x01` (EXECUTE) and the 4-byte big-endian length.
2. Opening a bidi stream, writing the bytes, finishing send.
3. Reading status byte + 4-byte length + payload bytes.

No payload mutation is required. This makes the migration in `zc`'s `src/broker/worker.rs` an incremental change — the existing cloudpickle handling stays, only the transport swaps.
