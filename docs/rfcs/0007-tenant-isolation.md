# RFC 0007 — Tenant isolation: Docker + ephemeral single-tenant workers

- **Status:** Accepted (2026-05)
- **Closes:** [#136](https://github.com/zakuro-ai/zakuro/issues/136)
- **Depends on:** RFC 0001 (postcard envelope + HMAC over the cloudpickle body), RFC 0002 (mTLS + JWT tenant claims), RFC 0004 (P2P, no Kubernetes)

## Context

Zakuro lets a tenant ship a Python callable (via `@zk.fn`) to a worker over the wire. The worker must isolate that callable from:

- the worker's host OS,
- other tenants ever served by the same worker,
- the worker operator itself (in the limited sense documented under §"Non-goals").

The earlier hardening backlog assumed the worker ran in a Kubernetes pod, so isolation was "namespace + network policy + seccomp default". RFC 0004 dropped Kubernetes; this RFC picks the replacement model.

The three constraints that shape the answer:

1. **Workers are operated by anyone.** A compute marketplace where any laptop / data-centre host can register and serve jobs. The operator is **not trusted** — they have full physical access to the hardware their workers run on.
2. **GPU access is first-class.** Every worker that advertises GPU capacity must actually expose it to the tenant's job. ML training / inference is the primary workload.
3. **Multi-platform for dev, Linux for prod.** Workers run on Linux x86_64 / arm64 in production. macOS / Windows are supported only for development.

## Decision

**Each worker is leased to one tenant at a time. The unit of isolation is the worker process, not the per-job container.** The lease has a TTL; on expiry the worker is destroyed and recycled. The runtime stack on Linux:

1. **Docker** as orchestration + packaging + GPU mgmt + cross-platform parity for dev.
2. **runc** as the OCI runtime by default — direct host kernel, required for working CUDA / ROCm. Hardened with the flags below.
3. **gVisor (runsc)** as an opt-in runtime for CPU-only worker pools where the perf hit (~5-15 %) is acceptable for a stronger syscall sandbox.
4. **Ephemeral worker lifecycle.** A worker is created, leased, runs N jobs, expires, is destroyed. State below the worker boundary is discarded between leases.

GPU workloads stay on runc (gVisor's `--nvproxy` is experimental and incomplete). The single-tenant lease gives the missing isolation: a malicious tenant cannot snoop another tenant on the same GPU because no other tenant is on that GPU at the same time.

### Worker pools

The broker routes jobs to the matching pool. Every worker advertises its pool in `/info` (already wired):

| Pool | OCI runtime | Hardening flags | GPU? | Trust assumption | Use |
|---|---|---|---|---|---|
| `gpu-strict` | runc | full hardening (see below) | ✓ NVIDIA/ROCm | adversarial; **isolation = lease boundary** | ML inference / training |
| `cpu-strict` | runc | full hardening | ✗ | adversarial; lease boundary + ephemeral | classical CPU jobs |
| `cpu-sandbox` | **runsc** (gVisor) | full hardening | ✗ | adversarial; lease + syscall sandbox | hardened CPU jobs, defence-in-depth |

The `cpu-sandbox` pool is opt-in via tenant tier or job-level flag (`zk.Compute(..., sandbox="gvisor")`). Default for `@zk.fn` decorators with no explicit sandbox is `cpu-strict` or `gpu-strict` based on `resource_limits.gpus`.

### Docker hardening flags (every pool)

These are applied uniformly. They give the *outer* containment; the *inner* containment is the lease boundary itself.

```bash
docker run \
    --rm \
    --runtime=${ZAKURO_OCI_RUNTIME:-runc} \              # runsc for cpu-sandbox
    --read-only \                                         # no writes to rootfs
    --tmpfs /tmp:rw,nosuid,nodev,size=512m \              # ephemeral writable scratch
    --tmpfs /var/run:rw,nosuid,nodev,size=8m \
    --cap-drop=ALL \                                      # no Linux capabilities
    --security-opt=no-new-privileges:true \               # block setuid escalation
    --security-opt=seccomp=$ZAKURO_SECCOMP_PROFILE \      # custom strict profile (below)
    --pids-limit=2048 \                                   # fork-bomb defence
    --memory=${MEM} --memory-swap=${MEM} \                # no swap excursion
    --cpus=${CPUS} \
    ${GPU_FLAGS} \                                        # --gpus, see below
    --network=zakuro-tenant-${TENANT_ID} \                # per-tenant Docker network
    --user 65532:65532 \                                  # distroless nonroot (RFC §134)
    --userns-remap=zakuro-worker \                        # host uid != container uid
    --hostname=worker-${WORKER_ID} \
    zakuroai/zakuro-worker:${VERSION}-distroless \
    python -m zakuro.worker.server
```

A custom seccomp profile (`docker/seccomp-zakuro.json`) further restricts the default Docker profile by denying:

- `ptrace`, `kcmp`, `unshare`, `setns` — anti-debugger / anti-namespace-manipulation
- `mount` / `umount` — even though `--read-only` should make this moot
- `bpf`, `perf_event_open` — close side-channel observation primitives
- `kexec_load`, `kexec_file_load`, `reboot`, `init_module`, `delete_module`, `finit_module` — defence against kernel module load if user namespaces leak
- network ioctls beyond what `aioquic` needs

### GPU flags

For `gpu-strict`:

```bash
GPU_FLAGS="--gpus device=${WORKER_GPU_INDEX} \
           --device-cgroup-rule='c 195:* rmw' \           # NVIDIA char devices
           --runtime=nvidia"                              # nvidia-container-runtime
```

The `--gpus device=N` pins a specific physical GPU to the worker, so two workers on the same host can hold different GPUs. The broker's `/info` lookup returns `gpus.gpus_total = 1` per worker per GPU index (already wired in `zakuro/worker/server.py`).

ROCm support (AMD): same shape via `--device=/dev/kfd --device=/dev/dri --group-add=video`. Documented in [`docs/getting-started.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/getting-started.md) when the first AMD provider ships.

### Lease lifecycle

```
broker                     worker (ephemeral)
  │                              │
  ├── allocate(tenant=T) ────────►│       worker spawned with full
  │                              │       hardening, network=zakuro-tenant-T,
  │                              │       GPU pinned (if requested)
  │                              │
  │       exec(plan_1) ──────────►│
  │       result_1 ◄──────────────┤
  │       exec(plan_2) ──────────►│
  │       result_2 ◄──────────────┤
  │       ...                    │
  │                              │
  ├── release(lease_id) ─────────►│       SIGTERM → graceful shutdown
  │                              ✗       container destroyed
  │                                      anonymous volumes purged
  │                                      next worker on this physical
  │                                      slot spawned fresh
```

Lease TTL defaults to 1 hour or 1k jobs, whichever first. On expiry, the broker withdraws routing, the worker drains in-flight, the container exits, and a successor is spawned by the worker operator's supervisor (systemd / Docker Compose / hand-rolled). The new worker has a fresh anonymous volume, a fresh tenant network, and zero residue from the previous tenant.

### Lease bounding

A worker advertises its lease bounds in `/info`:

```json
{
  "worker_id": "...",
  "pool": "gpu-strict",
  "lease": {
    "max_ttl_seconds": 3600,
    "max_jobs":        1000,
    "current_ttl_remaining_seconds": 1837,
    "current_jobs_remaining":         842
  },
  ...
}
```

Adaptive allocator already tracks per-worker availability via heartbeat probes (see [`zakuro/adaptive.py`](https://github.com/zakuro-ai/zakuro/blob/master/zakuro/adaptive.py)); it stops routing new traffic to a worker whose lease is near expiry.

## Implementation plan

### Step 1 — codify the hardening flags

Land `docker/run-worker.sh`:

```bash
#!/bin/sh
exec docker run --rm \
    --runtime="${ZAKURO_OCI_RUNTIME:-runc}" \
    --read-only \
    ${TMPFS_FLAGS} \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --security-opt=seccomp="${ZAKURO_SECCOMP_PROFILE:-/etc/zakuro/seccomp.json}" \
    ${RESOURCE_FLAGS} \
    ${GPU_FLAGS} \
    ${NETWORK_FLAGS} \
    --user 65532:65532 \
    "zakuroai/zakuro-worker:${ZAKURO_VERSION}-distroless" \
    "$@"
```

Plus `docker/seccomp-zakuro.json` (start from Docker's default profile, deny the additional syscalls listed above, ship it in the image at `/etc/zakuro/seccomp.json`).

### Step 2 — lease state in the worker

Extend `zakuro/worker/server.py`:

- Two env vars `ZAKURO_LEASE_TTL_SECONDS`, `ZAKURO_LEASE_MAX_JOBS`.
- A FastAPI middleware tracks remaining ttl + job count, exposes them on `/info`.
- On exceeding either bound, server returns 410 Gone on subsequent `/execute` and exits 0 after the in-flight handler completes.

Broker side: `AdaptiveCompute` already has health probing; add a `lease.remaining_*` lookup so it drains the lease gracefully.

### Step 3 — pool routing

Worker `/info.pool` is populated from `ZAKURO_WORKER_POOL` env var. Default = `cpu-strict`; operators with GPU declare `gpu-strict`; tenants opt into `cpu-sandbox` via `zk.Compute(..., sandbox="gvisor")`.

Broker's allocator gains a pool filter; the existing softmax routing operates within the pool.

### Step 4 — gVisor toggle

The `cpu-sandbox` pool sets `ZAKURO_OCI_RUNTIME=runsc`. Operators of `cpu-sandbox` workers install `runsc` per [gVisor's instructions](https://gvisor.dev/docs/user_guide/install/) and register it as a Docker runtime. Documented in `docs/operator.md` (to be added).

### Step 5 — observer instrumentation

- Sentry tag `worker.pool` on every event (extends [RFC 0003](0003-observability-stack.md)'s `request_context`).
- Prometheus counter `zakuro_lease_exits_total{reason=ttl|jobs|signal|crash}` so operators can spot crash-loop tenants.
- structlog event `lease_started` / `lease_ended` per worker boot/shutdown, machine-readable for an "audit trail of leases" later.

## Non-goals (deliberately out of scope)

### Confidentiality from the worker operator

The worker operator owns the hardware. They can — by design — read RAM, log syscalls, dump GPU memory, MITM the Docker daemon. We **cannot** guarantee the tenant's data is hidden from them.

What this RFC **does** guarantee:

- **Code execution integrity.** The tenant's job runs the code the tenant sent, not a tampered version (signed envelope from RFC 0001).
- **Billing accuracy.** The broker tracks job count + duration; the operator cannot claim credit for work not performed (the tenant signs ACKs of completion).
- **Tenant-tenant non-interference.** No other tenant ran on this worker during this lease.

What it does **not** guarantee:

- **Plaintext data confidentiality from the operator.** A tenant who needs that must use **confidential computing** (Intel TDX, AMD SEV-SNP, NVIDIA H100 confidential compute) and accept that they're paying for hardware that supports it. This is a future tier (`pool=confidential`), out of scope for v1.

The tenant's product copy should be explicit: "Workers are operated by third parties. Do not send PII or regulated data to a general worker. For confidential workloads, request the `confidential` pool." Add that wording to the v0.5 release notes.

### Side-channel resistance on shared hardware

Two workers from different tenants on the same physical host (different GPUs, different cgroups) can still observe each other through cache timing, Rowhammer, memory bandwidth, thermal. Mitigation:

- Single-tenant GPU lease bounds the bandwidth side-channel on the GPU itself.
- Multi-worker / multi-tenant *on the same host* requires a policy choice (operator's call). Document, don't enforce.

### Anti-cryptojacking / fair-use policing

A tenant could submit a perfectly compliant job that does Monero mining. The seccomp profile doesn't stop arithmetic. This is an Acceptable Use Policy / billing problem, not a runtime isolation problem. Out of scope.

## Rejected alternatives

| Option | Why rejected |
|---|---|
| nsjail / firejail | Lighter than Docker but no GPU integration. Multi-platform story is worse (Linux-only). |
| Rootless containers (podman / crun) | Works but loses some primitives (cgroups v1 quirks, overlay2 fallback). Docker's user-namespace remapping covers the same threat for less ops cost in 2026. |
| Firecracker / Kata as default | No GPU passthrough (Firecracker) or 1:1 GPU-to-VM (Kata) — incompatible with the "GPU first-class" constraint. Kata remains a **future** opt-in pool for non-GPU adversarial workloads where syscall sandbox + VM is paranoid-tier. |
| WebAssembly runtime (wasmtime, wasmer) | Strong sandbox, no CUDA / ROCm story. Tenants would need to rewrite to WASM. Out of scope for a "decorate a Python function" pitch. |
| gVisor by default everywhere | The `--nvproxy` GPU shim is experimental and rejects some real CUDA programs. Cannot be the default on `gpu-strict` workers. |

## Migration / rollout

1. RFC merges.
2. `docker/run-worker.sh` + `docker/seccomp-zakuro.json` land in a v0.5 PR.
3. `zakuro/worker/server.py` learns the lease TTL / job count + the `/info.lease` block.
4. `AdaptiveCompute` learns to drain leases.
5. Broker gains the pool filter.
6. v0.5 release notes carry the explicit "operators are untrusted" language + the lease semantics.
7. Pre-existing workers (no lease) are still served by the broker via a `legacy` virtual pool; they fall off after a deprecation window.

## Open questions for implementation time

- **Lease length** : 1h / 1k jobs are guesses. Tighten / relax after measuring operator economics on the first marketplace run.
- **Worker recycle latency** : container start ~2 s on cold host. If we see lease-end → next-job latency hurt, pre-warm a pool of one extra worker per slot.
- **Cross-platform dev parity** : the operator who develops on macOS will run Docker-in-VM-on-macOS. Worker boots, but `--runtime=runsc` is unavailable and the seccomp profile is a no-op (the macOS Docker VM ignores host-level seccomp). The dev experience is "your worker runs, your prod doesn't behave exactly the same" — document, don't fight.
- **AMD ROCm seccomp** : the ROCm runtime needs additional syscalls beyond CUDA. The seccomp profile may need a per-vendor variant; defer until the first AMD provider lands.
- **Lease handoff vs hard kill** : on TTL expiry, kill or migrate? Migration is complex (re-pickling running state). v1 = hard kill at lease end with in-flight job re-queued by the broker. Migration is a v2 feature, post-customer-ask.
