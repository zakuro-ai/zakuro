# API stability

Zakuro has two layers of public surface:

## `zakuro.public` — stable

Everything under [`zakuro/public.py`](../zakuro/public.py) ships a stability guarantee:

- **Names** in `zakuro.public` will not be removed without at least two minor versions of deprecation warning.
- **Function signatures** may only change in backward-compatible ways (add optional parameters, widen types). A breaking change requires a major-version bump.
- **Behavioural guarantees** documented in the module docstrings are covered by tests.

If you want to pin against stability, import from `zakuro.public`:

```python
from zakuro.public import fn, AdaptiveCompute, Compute, Worker
```

Current stable surface (v0.3):

| Symbol | Since | Notes |
|---|---|---|
| `fn` | 0.2.0 | function decorator |
| `cls` | 0.2.0 | class decorator |
| `Fn` | 0.2.0 | decorator return type |
| `Compute` | 0.2.0 | single-worker target |
| `AdaptiveCompute` | 0.2.3 | Adam-style multi-worker allocator |
| `Worker` | 0.2.2 | zakuro-worker subprocess wrapper |
| `Config` | 0.2.0 | configuration object |
| `detect_backend` | 0.2.2 | standalone fallback helper |
| `is_standalone` | 0.2.2 | standalone fallback helper |

## `zakuro.*` (non-`public`) — everything else

Still supported, still useful, but subject to refactor without deprecation:

- Transport internals (`zakuro.processors.*`)
- Standalone fallback internals (`zakuro.standalone` private symbols)
- Worker server internals (`zakuro.worker.server`, `zakuro.worker.quic_server`)
- Worker-CLI plumbing (`zakuro.worker.cli`, `zakuro.worker.runner._*`)
- Any name starting with `_`

If you find yourself reaching into one of these, consider whether the
thing you need should be promoted to `zakuro.public` — open an issue.

## Deprecation policy

When a stable name is going away:

1. A `DeprecationWarning` is emitted on import/use.
2. The replacement is documented in the warning message and in this file.
3. The name stays functional for **at least two minor versions**. For
   example, if 0.5 deprecates a name, it must still work in 0.5 and 0.6;
   removal can happen no earlier than 0.7.
4. Breaking behaviour changes to a stable name require a major version
   bump (1.0 → 2.0) — no silent changes.

## Wire protocol

The QUIC wire protocol is tracked in [`PROTOCOL.md`](PROTOCOL.md). New
incompatible frame formats get a new ALPN string (`zk-worker-v2`) so
brokers and clients can negotiate.
