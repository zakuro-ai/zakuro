# RFC 0004 — Deployment model: P2P over QUIC, no Kubernetes

- **Status:** Accepted (2026-05)
- **Closes:** [#131](https://github.com/zakuro-ai/zakuro/issues/131) (k8s operator + CRDs — **won't fix**), [#132](https://github.com/zakuro-ai/zakuro/issues/132) (Helm chart — **won't fix**)
- **Amends:** [RFC 0002](0002-auth-mtls-jwt.md) (the cert-manager dependency in §"Internal CA on the deployment cluster" is dropped)

## Context

The earlier hardening backlog (#131, #132) assumed Kubernetes as the operational substrate — operator + CRDs to model nodes, Helm to package the release, cert-manager to issue mTLS certs. That assumption was reversed in the May 2026 strategy pass: Zakuro is now positioned as a **P2P runtime over QUIC**, not a k8s workload. The runtime should bootstrap itself, discover peers without an orchestrator, and require no cluster-side infrastructure to come up.

This RFC documents that pivot and lays out the replacement deployment story so the consequential RFCs (0002 CA, 0007 gossip, 0008 isolation) have a consistent model to build on.

## Decision

**Zakuro nodes self-bootstrap, mutually authenticate over mTLS, and join a P2P mesh via gossip over QUIC.** No Kubernetes, no Helm, no cert-manager. The deployment artefact is a single signed binary (broker) + a single signed wheel (worker), plus a one-shot bootstrap command.

The three substrates the earlier model leaned on are replaced as follows:

| Removed | Replaced by |
|---|---|
| **Kubernetes pods + Services** | OS processes registered in the mesh via gossip. Each node has a stable `node_id` derived from its long-lived signing key. |
| **cert-manager** | A built-in self-signed CA bootstrapped by the first node, with new nodes admitted via a one-time `zakuro join --token <secret>` flow (token is a short-lived JWT issued by an existing mesh member). Long-lived rotation handled by re-issuance during the gossip handshake. |
| **Helm chart** | A single `zakuro install` CLI command that writes a systemd unit (Linux) / launchd plist (macOS) and provisions the working directory + age key. For container deployments, `docker run zakuroai/zakuro-worker` plus env vars is the documented path. |

A team that *does* want to deploy on Kubernetes still can — the worker image runs in any container scheduler and the broker has no k8s-specific dependencies. But k8s is no longer the recommended path and we do not maintain k8s-specific artefacts in this repo.

## Implementation plan

### 1. Bootstrap CLI

A new entry point `zakuro init` lives in `zakuro/worker/cli.py`:

```bash
zakuro init                    # First node in a mesh — generates the CA
                               # root, the node's signing key, and a join
                               # token. Prints the token + the listening
                               # mesh address to stdout.

zakuro join --addr <seed:4433> --token <token>
                               # Subsequent nodes — call the seed over
                               # QUIC, present the token, receive a
                               # signed mTLS cert valid for 24 h, write
                               # it to ~/.zakuro/certs/.
```

The token is a 15-minute Ed25519-signed claim with `purpose: "mesh-join"`. The seed verifies the token, signs the new node's CSR, and registers the node in its local gossip view. The new node receives a snapshot of the current peer list and starts gossiping.

### 2. CA without cert-manager

A single per-mesh CA, rooted at the first node, stored encrypted under SOPS+age (see [docs/secrets.md](https://github.com/zakuro-ai/zakuro/blob/master/docs/secrets.md)).

- **Root key:** generated once by `zakuro init`, written to `secrets/ca-root.sops.yaml`. Operationally treated like an offline root — only loaded into memory during issuance.
- **Intermediate:** one per mesh-quorum member (3–5 nodes), rotated quarterly. The intermediate is what signs short-lived (24 h) node certs during the gossip handshake.
- **Trust anchor distribution:** every node ships with the previous-generation intermediate public key in its image; rotations are propagated via gossip so a node coming back online after a rotation can still verify its peers.

The RFC 0002 §"Internal CA on the deployment cluster" step is replaced by §"CA without cert-manager" below; the rest of RFC 0002 (JWT scopes, mTLS-at-transport, JWT verification middleware) survives unchanged.

### 3. Discovery: gossip, not DNS

A separate forthcoming RFC will pick the gossip protocol shape (closing #138). Summary: each node periodically pushes its peer view to a random subset of peers; failure detection via phi-accrual. No external Consul / etcd / DNS-SD.

For laptop-dev, `ZAKURO_SEED_ADDR` env var pins a seed; for production, the bootstrap CLI writes the discovered seed set into `~/.zakuro/peers.json`.

### 4. Upgrade story

- **Rolling, peer-driven.** Each node restarts when its binary changes. Mesh re-stabilises within ~30 s of the restart (gossip TTL).
- **No drain-then-replace** as in k8s. In-flight jobs are migrated to a healthy peer via the dispatcher's existing retry path (`AdaptiveCompute` already handles `ConnectionError` mid-flight).
- **Version skew.** Both the wire format (RFC 0001 postcard envelope) and the gossip protocol (RFC 0007) carry an explicit `version` field. Two consecutive minor versions are guaranteed to interoperate; a major bump requires a full mesh restart at the maintainer's choice of window.

### 5. Multi-arch + signed binaries

The release pipeline already publishes Cosign-signed multi-arch images (RFC implicit — see #121, #133, #134). The CLI binary is added as a third signed artifact: GitHub Release attaches `zakuro-linux-{amd64,arm64}` and `zakuro-darwin-{amd64,arm64}`, each with a SLSA L3 attestation. Verification:

```bash
slsa-verifier verify-artifact \
    --provenance-path zakuro-linux-amd64.intoto.jsonl \
    --source-uri github.com/zakuro-ai/zakuro \
    --source-tag vX.Y.Z \
    zakuro-linux-amd64
```

## Rejected alternatives

| Option | Why rejected |
|---|---|
| Keep Kubernetes as the recommended path | Imposes operator burden (cluster, kubelet, control plane) on every Zakuro consumer. Goes against the "decorate a function, ship it" pitch in the [runtime tracking board's product pitch](https://github.com/orgs/zakuro-ai/projects/4). |
| Standalone binary + manual peer config (no gossip) | Works for ≤3 nodes; collapses at 10+. The mesh value-prop assumes auto-discovery. |
| Nomad / HashiCorp stack | Lighter than k8s, but still external orchestrator. The P2P story is more compelling commercially (operates anywhere, no cluster required). |
| libp2p as the transport | Mature P2P stack but pulls in DHT, NAT-traversal, content-routing layers we don't need yet. QUIC + custom gossip stays leaner. Re-evaluate if NAT-traversal becomes a customer ask. |

## Migration / rollout

1. This RFC merges.
2. #131 (k8s operator) and #132 (Helm chart) are closed as **won't fix** with a back-link to this RFC.
3. RFC 0002 is amended: the cert-manager paragraph is replaced by the §"CA without cert-manager" model above.
4. RFC 0007 (mesh gossip) lands separately — protocol details. This RFC commits us to *having* gossip; 0007 picks the wire shape.
5. RFC 0008 (tenant isolation) lands separately — given no pods, isolation moves to per-process. Same dependency chain.
6. The CLI bootstrap (`zakuro init`, `zakuro join`) ships in a v0.4 release; existing docker-compose flow remains supported for one minor.

## Open questions for implementation time

- **NAT traversal.** Pure P2P over QUIC works on LAN and on internet-facing nodes with public IPs. Cross-NAT mesh members may need an embedded TURN-style relay node. Defer until the first customer asks for a behind-NAT cluster.
- **Multi-mesh federation.** Joining two existing meshes is non-trivial (CA roots differ). Out of scope for v1; document as a deliberate non-goal.
- **Operator-style automation for power users.** Some buyers (k8s-native shops) will still want a k8s operator. If demand surfaces, the operator becomes a *third-party* repo that wraps the `zakuro init/join` CLI, not a first-party deliverable.
