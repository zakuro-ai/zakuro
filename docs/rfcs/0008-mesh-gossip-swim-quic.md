# RFC 0008 — Mesh gossip: SWIM over QUIC, postcard messages

- **Status:** Accepted (2026-05)
- **Closes:** [#138](https://github.com/zakuro-ai/zakuro/issues/138)
- **Depends on:** RFC 0001 (postcard wire format), RFC 0002 (mTLS + JWT for join admission), RFC 0004 (P2P deployment), RFC 0007 (worker pool advertisement carried in gossip payload)

## Context

RFC 0004 committed Zakuro to a P2P mesh over QUIC and explicitly punted on the gossip protocol. The remaining decision is *which* gossip protocol — Hashicorp memberlist, libp2p gossipsub, an academic protocol (Hyparview + plumtree), or a custom implementation.

The user's May 2026 call selected **custom postcard-over-QUIC + SWIM-style failure detection**. The rationale carried through every other RFC :

- QUIC is mandatory transport everywhere (RFC 0004).
- postcard is mandatory wire format everywhere (RFC 0001).
- mTLS + JWT already handles peer identity (RFC 0002).
- Workers are operated by anyone (RFC 0007) — adversarial trust model already shaped.

A custom protocol is **~500 LOC of well-understood algorithm** that reuses all of those layers; no FFI, no second serialization format, no UDP/TCP detour, no DHT/content-routing surface we don't yet need.

## Decision

**SWIM (Scalable Weakly-consistent Infection-style Process group Membership) over QUIC streams, with phi-accrual failure detection layered on top, and postcard-encoded message types.** Implemented as a standalone crate `crates/zakuro-gossip` consumed by the worker (Rust extension) and bound into Python via the existing pyo3 surface.

The pieces:

- **Membership state** — every node holds `HashMap<NodeId, NodeState>` where `NodeState` is `{Alive(incarnation), Suspect(incarnation, since), Dead(incarnation, since), Left}`. The incarnation number prevents stale gossip from reviving a left node.
- **Failure detection** — phi-accrual: for each peer, track inter-arrival of acks/heartbeats in a sliding window; compute φ; mark suspect when `φ > φ_suspect` (default 8.0) and dead when `φ > φ_dead` (default 12.0). Adapts to network jitter better than a fixed TTL.
- **Probing** — every `interval` (default 250 ms), pick a random peer `P` and send `Ping`. If no `Ack` within `probe_timeout` (default 500 ms), send `PingReq` to `K=3` random other peers asking them to probe `P` on our behalf. Phi-accrual is the source of truth; pings are how we feed it.
- **Dissemination** — every gossip message piggybacks a bounded list of recent state changes (`Delta`). State propagates epidemic-style; a change reaches all `N` nodes in roughly `O(log N)` rounds.
- **Bootstrap** — a new node calls `zakuro join --addr <seed> --token <jwt>` (RFC 0004). The seed verifies the token, returns its current `Membership` snapshot, and adds the joiner to its local view. The joiner immediately starts gossiping.

## Wire shape

All messages are postcard-encoded `serde` structs in `crates/zakuro-gossip/src/wire.rs`. Major variants:

```rust
#[derive(Serialize, Deserialize, Debug)]
pub enum GossipMessage {
    /// Periodic probe. Sender expects Ack.
    Ping { from: NodeId, seq: u64, deltas: Vec<Delta> },

    /// Sender asks the receiver to probe `target` on its behalf.
    PingReq { from: NodeId, target: NodeId, seq: u64, deltas: Vec<Delta> },

    /// Reply to Ping or successful PingReq.
    Ack { from: NodeId, seq: u64, deltas: Vec<Delta> },

    /// Push-style — sender forces a state delta into the mesh.
    /// Used for self-state changes (joining, leaving, pool re-advertise).
    Push { from: NodeId, deltas: Vec<Delta> },

    /// Bootstrap response: full membership snapshot to a new joiner.
    Snapshot { from: NodeId, members: Vec<(NodeId, NodeState, NodeMeta)> },
}

#[derive(Serialize, Deserialize, Debug)]
pub struct Delta {
    pub node: NodeId,
    pub state: NodeState,
    pub incarnation: u64,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct NodeMeta {
    pub addr: SocketAddr,          // QUIC listening addr
    pub pool: String,              // "gpu-strict" | "cpu-strict" | "cpu-sandbox" (RFC 0007)
    pub gpus: u8,
    pub cpus: f32,
    pub memory_mb: u64,
    pub lease_remaining_seconds: u64,  // for AdaptiveCompute, see RFC 0007
    pub version: String,           // semver of zakuro binary
}
```

Every QUIC stream used for gossip is a unidirectional sub-stream multiplexed on the existing mTLS-authenticated QUIC connection between two peers. The mTLS handshake guarantees the sender's `NodeId` matches the certificate subject (RFC 0002); a node that forges a `NodeId` in a `Ping` payload but presents a cert for a different identity is dropped at the stream boundary.

The `deltas` field is bounded at 32 entries per message — large enough for full state propagation in `O(log N)` rounds at typical mesh sizes, small enough that one UDP datagram carries a `Ping`.

## Anti-Sybil + adversarial-mesh defences

RFC 0004 admits joiners via a one-time `mesh-join` JWT signed by an existing quorum member. This RFC adds three runtime defences on top:

1. **Incarnation-rooted state.** Every state announcement carries the *announcing* node's incarnation number. A node `B` cannot mark node `A` as dead with `A`'s incarnation field arbitrarily; `B` must use the latest incarnation it has observed for `A`. A rebuttal `Alive` from `A` always wins because `A` bumps its own incarnation on rebuttal.
2. **Quorum-acknowledged removal.** A `Dead` delta is accepted into local state only when independently observed (probe failure or `PingReq` failures or other peers' `Dead` deltas) from at least `quorum = ceil(K/2) + 1` distinct sources within a window. Prevents one malicious node from kicking honest peers out of the mesh.
3. **Reputation downgrade.** A peer that gossips state which conflicts with majority view (e.g. announces peer `X` as dead while a majority of probes show `X` alive) is itself marked `Suspect` on a configurable strikes-out policy. Documented but not strictly required for v1 — the join-admission JWT is the primary defence; reputation is defence-in-depth.

## Sample tick

```
node_A every 250 ms:
  - sample peer P from membership where state ∈ {Alive, Suspect}
  - QUIC_send(P, Ping { seq, deltas: recent_changes(32) })
  - on Ack or no-Ack:
        update phi_accrual_window[P]
        if phi(P) > 8.0 and state(P) == Alive: state(P) = Suspect
        if phi(P) > 12.0 and state(P) != Dead: state(P) = Dead
  - if no Ack:
        choose 3 random peers Q_1, Q_2, Q_3 distinct from P
        for each Q_i:
            QUIC_send(Q_i, PingReq { target: P, seq })
        wait probe_timeout
        if any Q_i replies with Ack about P: state(P) = Alive
        else: phi_accrual feed counts as missed
  - on every Ack/Ping/PingReq received:
        merge deltas with local view using incarnation precedence
        clamp delta list back to ≤ 32 entries
```

## Implementation plan

### Step 1 — `crates/zakuro-gossip` skeleton

- New workspace member.
- `pub struct Gossip` with `new(node_id, listen_addr, seeds: Vec<SocketAddr>) -> Self`.
- `pub async fn tick(&self)` driven by an external scheduler (the worker's runtime).
- `pub fn membership(&self) -> Membership` — snapshot getter for the broker/adapter.
- Unit tests: in-process N-node simulation (use a fake transport that swaps `Ping`/`Ack` between local `Gossip` instances). Verify convergence, partition recovery, sybil rejection.

### Step 2 — QUIC integration

Reuse `crates/sakura-wire` (or its zakuro-side equivalent once that lands per RFC 0001) to open and accept QUIC streams. A long-lived QUIC connection per peer pair; gossip uses unidirectional streams (one stream per message), authenticated by the connection's mTLS cert.

A worker binary opens its QUIC listener once (broker + RPC + gossip share the same QUIC endpoint, different stream IDs).

### Step 3 — Python binding

Pyo3 wrapper in `zakuro/_gossip/`:

```python
from zakuro._gossip import Gossip

g = Gossip.new(
    node_id="worker-3",
    listen_addr="0.0.0.0:4433",
    seeds=["seed.zakuro.internal:4433"],
)
# Background task driven by asyncio
asyncio.create_task(g.run())

# Read snapshot for AdaptiveCompute / broker:
members = g.members()      # -> list[NodeMeta]
healthy_gpu = [m for m in members if m.state == "Alive" and m.pool == "gpu-strict"]
```

### Step 4 — Broker integration

The broker subscribes to membership changes via a callback. `AdaptiveCompute` already exposes `add_worker` / `remove_worker`; wire `Gossip` to call those on `Alive`/`Dead` transitions for nodes in the pool the client cares about.

### Step 5 — Observability hooks (RFC 0003)

- Counter `zakuro_gossip_messages_total{kind=ping|ack|pingreq|push|snapshot, direction=in|out}`.
- Histogram `zakuro_gossip_membership_size`.
- Histogram `zakuro_gossip_failure_detection_phi`.
- Structured logs on every state transition (`event=node_state_changed`, `node`, `from`, `to`, `incarnation`).
- OTel span `zakuro.gossip.tick` per probe round; child span per outbound `Ping`.
- Sentry tag `mesh.size` on every captured event.

### Step 6 — Configuration knobs

All defaults are conservative; tune after the first real-world deployment.

| Env var | Default | Description |
|---|---|---|
| `ZAKURO_GOSSIP_INTERVAL_MS` | 250 | Probe period |
| `ZAKURO_GOSSIP_PROBE_TIMEOUT_MS` | 500 | Direct Ping timeout |
| `ZAKURO_GOSSIP_INDIRECT_K` | 3 | Number of PingReq peers |
| `ZAKURO_GOSSIP_PHI_SUSPECT` | 8.0 | Phi threshold → Suspect |
| `ZAKURO_GOSSIP_PHI_DEAD` | 12.0 | Phi threshold → Dead |
| `ZAKURO_GOSSIP_DELTA_BUDGET` | 32 | Max state changes piggybacked per message |
| `ZAKURO_GOSSIP_SEEDS` | `""` | Comma-sep list of `host:port` |

## Rejected alternatives

| Option | Why rejected |
|---|---|
| Hashicorp memberlist (Go via FFI) | Pulls a Go runtime into the Rust pile, FFI maintenance burden, UDP/TCP-native — would need a QUIC shim. Battle-tested but the cost-of-import is higher than re-implementing 500 LOC of SWIM. |
| libp2p gossipsub | Powerful (DHT, NAT traversal, pub/sub topics) but the surface is bigger than what membership + health needs. Reconsider in v2 if cross-NAT mesh becomes a requirement. |
| Hyparview + plumtree | Academically nicer for high-churn topologies; few production-grade Rust implementations exist in 2026. Bringing it home would mean writing the same novel code we'd write for SWIM, with less industry precedent. Defer until a customer needs the topology robustness. |
| Static seed-set, no gossip (punt to v2) | Works up to ~50 nodes. Crosses-the-room before that — provider-marketplace pitch demands self-discovery. Punting was an option in the question round; rejected by the user for v1. |

## Migration / rollout

1. RFC merges.
2. `crates/zakuro-gossip` skeleton lands (Step 1). No runtime callers yet.
3. QUIC integration (Step 2). Hooked behind a feature flag `ZAKURO_MESH_ENABLED=true`.
4. Python binding (Step 3) + broker integration (Step 4). Behind the same flag.
5. Observability hooks (Step 5).
6. v0.6 release: `ZAKURO_MESH_ENABLED=true` by default. Static `ZAKURO_SEED_ADDR` config still works for single-mesh deployments.
7. v0.7: legacy static-seed mode deprecated. Mesh-mode is the only path.

## Open questions for implementation time

- **Multi-mesh federation.** Joining two pre-existing meshes (CA roots differ, NodeId may collide). RFC 0004 already declares this out-of-scope for v1; restate.
- **NAT traversal.** Workers behind NAT cannot accept QUIC streams; they could initiate outbound only. A relay-node story (TURN-style) becomes necessary when the first behind-NAT marketplace member ships. Out of scope for v1.
- **Anti-flood.** A malicious peer sending 10 k pings/sec must be rate-limited at the QUIC stream level. Use `quinn`'s built-in `max_concurrent_uni_streams`; tune.
- **Membership snapshot size at scale.** At `N = 10 000` nodes, a full `Snapshot` is ~1 MB postcard-encoded. Chunked transfer or push-only updates after bootstrap might be needed. Re-evaluate when `N > 1000` in a real deployment.
- **GPU re-advertisement frequency.** A worker's `lease_remaining_seconds` ticks every second; rebroadcasting that field via gossip every tick would saturate. Compromise: rebroadcast lease state only when it crosses a coarse threshold (e.g. 5-minute buckets), and let the broker query a worker directly via QUIC RPC for fresh values when about to dispatch.
