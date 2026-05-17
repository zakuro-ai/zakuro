# Threat model — STRIDE + LINDDUN

**Status:** Draft (2026-05). Maintainer: security@zakuro.ai. Refresh cadence: quarterly, or sooner when an RFC lands that materially shifts a trust boundary.

This document is the single shared mental model for what we are defending against in Zakuro. It is consulted at PR review (does this change cross a trust boundary?), at RFC time (does the proposed design address the existing threats?), and at audit time (what is in and out of scope?).

## 1. Scope

Covered:

- **Zakuro runtime** — the client library (`zakuro/`), the worker (`zakuro.worker.*`), the QUIC and HTTP transports.
- **`zc` broker** — the Rust broker at [`zakuro-ai/zc`](https://github.com/zakuro-ai/zc), via its on-the-wire contract with Zakuro (the `zakuro-wire` crate).
- **Release pipeline** — the GitHub Actions workflows that build, sign, and publish wheels + container images.
- **`zakuro-image` deliverables** — Cosign signing, SLSA L3 attestations, SOPS-encrypted runtime secrets. Per-image hardening lives in the image repo's threat-model section.

Out of scope:

- The user's training code itself. We treat the workload as **adversarial** — the worker runs it in an isolated tenant, but we do not guarantee the workload is correct. See [RFC 0007](../rfcs/0007-tenant-isolation.md).
- Hosted-broker SaaS (`my.zakuro-ai.com`). Operated separately, modelled separately.
- Hardware side-channels (Spectre, Rowhammer). Mitigation is the responsibility of the host / kernel.
- Physical access to the worker host.

## 2. System diagram

```
                       (untrusted network)
   ┌──────────┐    mTLS+JWT    ┌──────────┐    mTLS+JWT    ┌──────────┐
   │  client  │ ─────────────► │  broker  │ ─────────────► │  worker  │
   │ (Python) │                │   (zc)   │                │ (Python) │
   └────┬─────┘                └────┬─────┘                └────┬─────┘
        │                            │                            │
        │                            ▼                            ▼
        │                    ┌────────────┐               ┌─────────────┐
        │                    │ Postgres   │               │ workload    │
        │                    │  (ledger)  │               │   (adv.)    │
        │                    └────────────┘               └──────┬──────┘
        │                                                        │
        ▼                                                        ▼
   ┌──────────┐                                          ┌─────────────┐
   │  user    │                                          │ S3/storage  │
   │  code    │                                          │  (MinIO)    │
   └──────────┘                                          └─────────────┘
```

**Trust boundaries** (crossings require explicit auth + integrity checks):

| Boundary | Crossing | Auth | Integrity |
|---|---|---|---|
| TB-1 | client → broker | mTLS + JWT (RFC 0002) | postcard envelope + HMAC (RFC 0001) |
| TB-2 | broker → worker | mTLS + JWT | postcard envelope + HMAC |
| TB-3 | worker → workload | OS isolation (Docker + gVisor / process boundary) | — |
| TB-4 | worker → object store | S3 SigV4 + per-tenant credential | server-side checksum |
| TB-5 | maintainer → release | GitHub OIDC + cosign keyless | SLSA L3 provenance |
| TB-6 | mesh peer ↔ peer | mTLS over QUIC (RFC 0008) | gossip msg signature |

## 3. STRIDE per component

### 3.1 Client library (`zakuro/`)

| Threat | Vector | Mitigation | Residual risk |
|---|---|---|---|
| **S**poofing | Stolen JWT replayed by a different process | mTLS pins peer cert; JWT `aud` binds it to the broker URL | Low — replay window ≤ JWT TTL (15 min) |
| **T**ampering | Modified wheel installed from a typo-squat | NOTICE drift-check; verifying-releases.md cosign snippet | Medium — depends on user verifying |
| **R**epudiation | Client denies submitting a job after the fact | Broker logs every accepted job with the client's cert subject CN | Low |
| **I**nfo disclosure | Job payload leaks via pip telemetry / Sentry | PII scrubber in `zakuro.observability.sentry`; structlog redaction (RFC 0003) | Medium — workload may emit secrets itself |
| **D**oS | Client floods broker with malformed envelopes | Broker rate-limits per-cert; postcard parser is `O(n)` and bounded | Low |
| **E**levation | `@zk.fn`-decorated function escapes the client process | Function runs in the same process as the caller (by design) | N/A (no escalation possible — workload is the user's own code on their own host) |

### 3.2 Broker (`zc`)

| Threat | Vector | Mitigation | Residual risk |
|---|---|---|---|
| **S** | Worker impersonates another worker to receive jobs | Worker mTLS cert pins `worker-id`; JWT `worker_id` claim must match | Low |
| **T** | In-flight envelope altered between client and worker | postcard envelope HMAC (RFC 0001); HKDF-derived per-tenant key | Low |
| **R** | Broker denies routing a job after a worker failed | Postgres double-entry ledger (RFC 0005) is the system of record | Low |
| **I** | Tenant A's job metadata leaks to tenant B via shared logs | per-tenant log filtering (RFC 0003 §Tenant tag); ledger queries scoped by `tenant_id` | Medium — operator sees all tenants by design |
| **D** | One tenant exhausts the broker's worker pool | Per-tenant credit ledger + quota gate in the broker | Medium — quota is best-effort under burst |
| **E** | Broker bug allows a tenant to call `admin:credits` scope | JWT scope check on every handler; fuzz the scope-parsing path | Low |

### 3.3 Worker (`zakuro.worker.*`)

| Threat | Vector | Mitigation | Residual risk |
|---|---|---|---|
| **S** | Spoofed broker dispatches a job | mTLS pins broker cert; JWT issuer must be the cluster's broker key | Low |
| **T** | Workload binary swapped between dispatch and execution | Worker hashes the function bytes on receipt; HMAC over the envelope covers args + callable | Low |
| **R** | Worker denies running a job | Structured logs forward to the cluster log sink; ledger records the worker's signed receipt | Low |
| **I** | Workload reads `ZAKURO_AUTH` from env and exfiltrates it | Worker drops auth env vars before `exec(workload)`; per-tenant ephemeral lease (RFC 0007) | **High** if GPU multi-tenant; **Low** in single-tenant GPU mode (the documented default) |
| **D** | Malicious workload spawns fork bombs / fills disk | gVisor (`runsc`) syscall sandbox; cgroups CPU+memory caps; `--pids-limit`; `--read-only` rootfs | Medium — gVisor coverage is wide but not total |
| **E** | Workload escapes the gVisor sandbox | gVisor is the primary boundary; defence in depth via dropped capabilities, seccomp profile, non-root user | Medium — gVisor escapes have been published historically; tracked at quarterly review |

### 3.4 Release pipeline

| Threat | Vector | Mitigation | Residual risk |
|---|---|---|---|
| **S** | Attacker pushes a tag from a stolen maintainer credential | Branch protection + required reviews; cosign signature ties artefact to GitHub OIDC identity | Low |
| **T** | Compromised dependency injects code at build time | `osv-scanner`, `pip-audit`, Dependabot weekly rollups; SBOM attestation lets downstream verify | Medium — supply-chain attacks remain the highest-residual class |
| **R** | Maintainer denies cutting a release | Signed tag + Rekor transparency log entry per artefact | Low |
| **I** | Build env leaks `PYPI_API_TOKEN` | Trusted Publishing (OIDC) — no long-lived tokens in GH secrets | Low |
| **D** | CI bottlenecks block emergency security release | Manual `workflow_dispatch` path + documented offline-sign procedure | Low |
| **E** | Workflow privilege escalation via untrusted PR | `pull_request_target` is not used; `permissions:` is minimised per workflow | Low |

## 4. LINDDUN privacy threats

| Threat | Description | Mitigation |
|---|---|---|
| **L**inkability | Two requests from the same user can be tied across tenants | Per-tenant pseudonymous IDs; no global user ID on the wire |
| **I**dentifiability | Logs contain PII (emails, IPs, file paths) | structlog PII redactor + Sentry `_redact_pii_in_string` (RFC 0003) |
| **N**on-repudiation | A user cannot disclaim an action they did not take | Audit log retention bounded; users can request deletion via `dev@zakuro.ai` (GDPR Article 17) |
| **D**etectability | An attacker can detect whether a tenant is active | Single shared TLS handshake on the broker (no per-tenant SNI leak); broker does not echo tenant existence in 401 responses |
| **D**isclosure of information | Workload state-dicts leak through telemetry | Telemetry covers control-plane only; data-plane payloads never go to Sentry / Prom labels |
| **U**nawareness | User does not know what data Zakuro retains | Documented in `docs/privacy.md` (placeholder — to be written before any prod traffic from EU customers) |
| **N**on-compliance | Conflict with GDPR / SOC2 / HIPAA | SOC 2 / ISO 27001 control mapping tracked in [#139](https://github.com/zakuro-ai/zakuro/issues/139); HIPAA explicitly out-of-scope until a customer asks |

## 5. Adversary model

We design for the following adversaries, in order of capability:

1. **Curious peer** — another tenant on the same broker. **Goal:** read another tenant's job metadata or output. **Defence:** mTLS + JWT scopes, per-tenant credit ledger, no shared state.
2. **Compromised worker** — an attacker has remote code execution inside a worker (e.g. via a workload-side bug). **Goal:** pivot to a sibling worker or escalate to the broker. **Defence:** ephemeral worker leases (RFC 0007), short-lived JWTs, no long-lived secrets in worker memory, gVisor sandbox.
3. **Network attacker** — controls the link between client/broker/worker. **Goal:** read or modify in-flight jobs. **Defence:** mTLS at QUIC, postcard HMAC, JWT integrity.
4. **Malicious operator** — runs the worker host. **Goal:** read tenant data the worker is processing. **Defence:** **none in v1.** Confidential computing (SEV-SNP / TDX) is the only mitigation and is explicitly out-of-scope for v1; documented as a known limitation in [RFC 0007](../rfcs/0007-tenant-isolation.md).
5. **Supply-chain attacker** — compromises a third-party dependency. **Goal:** code execution in every Zakuro install. **Defence:** SBOM, `osv-scanner`, signed releases, Dependabot rollups. Residual risk is non-zero and tracked at every release review.

## 6. Open risks (deliberate)

| Risk | Why we accept it | Re-evaluate when |
|---|---|---|
| Workloads can read each other's GPU memory across context switches | Single-tenant GPU pool is the documented mode; multi-tenant on shared GPU is opt-in via flag with a documented warning | A customer asks for multi-tenant GPU without per-tenant attestation |
| gVisor escape windows | gVisor is the best general-purpose option; the alternative (full VM per job) costs >10× dispatch latency | A gVisor CVE we cannot patch within 7 days drops |
| Operator can read tenant traffic in plaintext on the worker host | Confidential computing is not v1 | A customer with regulatory needs (FedRAMP High, HIPAA BAA) commits |
| Cross-mesh federation has no defined trust model | v1 is single-mesh by design (RFC 0004) | Two-customer-mesh-bridge becomes a real ask |

## 7. Process

- Every RFC must include a "Threats considered" section pointing back here.
- Every PR that crosses a trust boundary in §2 must annotate which TB-N it touches and why the mitigation still holds.
- Every quarter, security@zakuro.ai opens an issue titled "Threat model refresh — YYYY-QN" that re-walks §3 and §6 and updates this file. Closing that issue is gated on a maintainer sign-off.
- A successful external pentest report (tracking [#143](https://github.com/zakuro-ai/zakuro/issues/143)) is appended to §6 as a dated annex.

## 8. References

- RFC 0001 — postcard wire format + HMAC
- RFC 0002 — mTLS + JWT scopes
- RFC 0003 — observability + PII redaction
- RFC 0004 — P2P deployment (drops cert-manager)
- RFC 0007 — tenant isolation
- RFC 0008 — mesh gossip auth
- [`docs/security/verifying-releases.md`](verifying-releases.md) — supply-chain verification flow
- [Microsoft STRIDE](https://learn.microsoft.com/en-us/azure/security/develop/threat-modeling-tool-threats), [LINDDUN](https://www.linddun.org/) — methodology references
