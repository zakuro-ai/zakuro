# API stability

Zakuro has two layers of public surface:

## `zakuro.public` — stable

Everything under [`zakuro/public.py`](https://github.com/zakuro-ai/zakuro/blob/master/zakuro/public.py) ships a stability guarantee:

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
| `replay_decisions` | 0.3 | parse an allocator decision log into a summary |
| `DecisionLogSummary` | 0.3 | return type of `replay_decisions` |

`AdaptiveCompute` gained three optional constructor parameters in 0.3 —
`seed`, `max_dispatch_retries`, `eject_after_failures` — all additive and
defaulting to the prior behaviour. The `zakuro` console script (`init`,
`doctor`, `config get`, `allocator replay`) is a tool, not an importable
API, so it is versioned with the package but not listed in this table.

## `zakuro.*` (non-`public`) — everything else

Still supported, still useful, but subject to refactor without deprecation:

- Transport internals (`zakuro.processors.*`)
- Standalone fallback internals (`zakuro.standalone` private symbols)
- Worker server internals (`zakuro.worker.server`, `zakuro.worker.quic_server`)
- Worker-CLI plumbing (`zakuro.worker.cli`, `zakuro.worker.runner._*`)
- Any name starting with `_`

If you find yourself reaching into one of these, consider whether the
thing you need should be promoted to `zakuro.public` — open an issue.

## Semantic versioning

Zakuro follows [SemVer 2.0](https://semver.org/) for the public surface
listed above (`zakuro.public`).

| Bump | Allowed | Disallowed |
|---|---|---|
| **patch** (0.3.0 → 0.3.1) | Bug fixes, perf improvements, doc-only changes, **no** change in public-API behaviour. | Adding, renaming, removing, or relocating a public name. |
| **minor** (0.3 → 0.4) | New public names, optional parameters, widened types. Deprecation **start** for an existing public name. | Removing or breaking-changing a public name. |
| **major** (0.x → 1.0, 1.x → 2.0) | Removing deprecated names, breaking signature / behaviour changes. | Silent breakage — every breaking change is in the changelog. |

Pre-1.0 caveat: while the 0.x series is in pre-release, occasional minor
breaks may still happen on `zakuro.public`. They are flagged in the
release notes and the patch-version bump explicitly. The same rules
become strict from 1.0 onward.

## Deprecation policy

When a stable name is going away:

1. A `DeprecationWarning` is emitted on import/use, with the replacement
   in the message.
2. The replacement is documented here in the **Deprecation log** below.
3. The name stays functional for **at least two minor versions** — e.g.
   a name deprecated in 0.5 must still work in 0.5 and 0.6, removal no
   earlier than 0.7.
4. Breaking behaviour changes to a stable name require a major version
   bump — no silent changes.

Downstream CI can lock this in with:

```bash
pytest -W error::DeprecationWarning
```

### Deprecation log

| Symbol | Deprecated in | Removal target | Replacement |
|---|---|---|---|
| _(none yet)_ | | | |

## 1.0 LTS window

The first 1.0 release will become a **Long-Term Support** branch with
the following commitment:

- **24 months** of bug + security fixes after 1.0 ships.
- During this window, `zakuro.public` is frozen — only patch-level
  changes, no deprecations.
- The next major (2.0) may begin parallel pre-release work after 1.0
  ships; the two coexist until 1.0 LTS ends.

This window is published here, not buried in release notes, so
enterprise users can plan procurement around it.

## Type checking (PEP 561)

The wheel ships a `py.typed` marker, so `mypy` / `pyright` automatically
pick up the inline annotations on `zakuro.public`. Pin downstream type
checks against the stable surface with:

```bash
mypy --strict your_zakuro_using_code.py
```

If you find an unannotated public function, that's a bug — open an
issue.

## Wire protocol

The QUIC wire protocol is tracked in [`PROTOCOL.md`](PROTOCOL.md). New
incompatible frame formats get a new ALPN string (`zk-worker-v2`) so
brokers and clients can negotiate.
