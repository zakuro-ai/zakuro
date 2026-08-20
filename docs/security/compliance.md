# Compliance control mapping — SOC 2 (TSC 2017) + ISO 27001:2022 Annex A

**Status:** Draft (2026-05). Maintainer: security@zakuro-ai.com. Refresh cadence: quarterly, or sooner when a control implementation lands.

This document maps Zakuro's implemented and planned controls to the relevant SOC 2 Trust Services Criteria and ISO 27001:2022 Annex A control numbers. It is **not** an attestation — Zakuro has not been audited yet — but it is the worksheet a third-party assessor would consult when scoping engagement. Each row identifies the artifact that an assessor would test (workflow file, code path, RFC, doc) so a customer asking "show me how you handle X" gets a direct pointer rather than a marketing page.

Closes [#139](https://github.com/zakuro-ai/zakuro/issues/139).

## How to read this table

- **Status** — `implemented` = code shipped and in CI; `planned` = RFC accepted, implementation in flight; `gap` = no work in flight, deliberate or otherwise.
- **Evidence** — the path / RFC / issue an auditor would inspect. For controls implemented in `zakuro-ai/zakuro-image`, the evidence sits in that repo.
- **Owner** — the team that maintains the control. Single-team project today, so all rows say `maintainers`; this column exists so the table survives org growth.

## SOC 2 — Trust Services Criteria

The 2017 TSC framework groups controls into Common Criteria (CC1–CC9) plus four discretionary categories (Availability A1, Confidentiality C1, Processing Integrity PI1, Privacy P1–P8). We claim alignment with **Common Criteria + Availability + Confidentiality** for v1; PI and Privacy are aspirational (see §"Gaps").

### CC1 — Control environment

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC1.1 Demonstrates integrity & ethical values | implemented | [`CODE_OF_CONDUCT.md`](https://github.com/zakuro-ai/zakuro/blob/master/CODE_OF_CONDUCT.md) (Contributor Covenant 2.1) | maintainers |
| CC1.2 Independent board oversight | gap | Pre-formal-governance startup; tracked as part of org-readiness | maintainers |
| CC1.3 Management establishes structure & reporting lines | implemented | [`CONTRIBUTING.md`](https://github.com/zakuro-ai/zakuro/blob/master/CONTRIBUTING.md) (reviewer expectations) | maintainers |
| CC1.4 Commits to competence | implemented | Reviewer ownership in `CONTRIBUTING.md`; PR-template checklist | maintainers |
| CC1.5 Holds individuals accountable | implemented | Signed commits required (per `docs/ci.md` branch-protection table) | maintainers |

### CC2 — Communication and information

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC2.1 Obtains and uses relevant information | implemented | RFC process (`docs/rfcs/`) — every architectural decision has a written record | maintainers |
| CC2.2 Communicates internally | implemented | GitHub Discussions + the merge-time review trail | maintainers |
| CC2.3 Communicates with externals (incl. customers) | partial | `SECURITY.md` (`security@zakuro-ai.com` channel), GitHub Security Advisories. Customer status-page is a gap. | maintainers |

### CC3 — Risk assessment

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC3.1 Specifies objectives with sufficient clarity | implemented | [Runtime tracking board (project #6)](https://github.com/orgs/zakuro-ai/projects/6) — phase status + measured numbers per release | maintainers |
| CC3.2 Identifies and analyses risks | implemented | [`docs/security/threat-model.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/security/threat-model.md) — STRIDE + LINDDUN with residual-risk per component | maintainers |
| CC3.3 Considers fraud risk | partial | Threat model §5 covers supply-chain + malicious operator; financial-fraud not yet (no payment surface) | maintainers |
| CC3.4 Identifies and assesses significant change | implemented | RFC process; threat-model refresh on every cross-boundary change | maintainers |

### CC4 — Monitoring activities

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC4.1 Selects, develops, and performs ongoing and/or separate evaluations | partial | CI runs continuously; external pentest is planned (#143). | maintainers |
| CC4.2 Evaluates and communicates deficiencies | implemented | GitHub Security Advisories + post-mortem template (forthcoming) | maintainers |

### CC5 — Control activities

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC5.1 Selects and develops control activities | implemented | `docs/ci.md` (lane catalogue), `docs/security/threat-model.md` (mitigations) | maintainers |
| CC5.2 Selects and develops general controls over technology | implemented | Required CI lanes: `Test`, `Build`, `Rust`, `Docs`, `notice`; signed commits; PR review | maintainers |
| CC5.3 Deploys through policies and procedures | implemented | Required-lane policy enforced via branch protection (`docs/ci.md`) | maintainers |

### CC6 — Logical and physical access controls

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC6.1 Implements logical access security | planned | [RFC 0002](../rfcs/0002-auth-mtls-jwt.md) — mTLS + JWT scopes; #115, #116, #117 | maintainers |
| CC6.2 Authorises and registers users | planned | RFC 0002 §"JWT issuance" — broker-issued, per-tenant, short-lived | maintainers |
| CC6.3 Removes and modifies access | planned | RFC 0002 §"Refresh" — 15-min TTL, no long-lived refresh tokens | maintainers |
| CC6.4 Restricts physical access | n/a | Project ships software; physical-access posture is the customer's host | n/a |
| CC6.5 Removes legacy systems & data | implemented | Release pipeline does not retain old container images beyond retention policy (90 days); SBOMs published per-release for traceability | maintainers |
| CC6.6 Restricts logical access to internal users | planned | RFC 0002 + RFC 0007 (tenant isolation) | maintainers |
| CC6.7 Restricts transmission of information | implemented | mTLS on every QUIC connection (RFC 0002 §2), TLS at HTTP transport | maintainers |
| CC6.8 Implements controls to prevent and detect unauthorised software | implemented | Cosign keyless image signing + Rekor (zakuro-image#18); SBOM emission (zakuro-image#17); SLSA L3 provenance | image-team |

### CC7 — System operations

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC7.1 Detects and prevents new vulnerabilities | implemented | `pip-audit`, `osv-scanner`, `trivy fs`, `trivy image`, Semgrep, CodeQL (all wired; see `docs/ci.md`) | maintainers |
| CC7.2 Monitors system components for anomalies | implemented | Sentry (#128) — unhandled errors; structlog JSON logs (#125); Prometheus metrics (#124); OTel tracing (#123) | maintainers |
| CC7.3 Evaluates security events | partial | Sentry alerts → on-call; SLO burn-rate alerts planned (#127) | maintainers |
| CC7.4 Responds to identified security events | implemented | `SECURITY.md` triage SLA + disclosure policy | maintainers |
| CC7.5 Recovers from incidents | partial | Restart-and-replace via systemd / docker-compose; full DR runbook is a gap | maintainers |

### CC8 — Change management

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC8.1 Authorises, designs, develops, tracks, tests, approves, and implements changes | implemented | Required PR review + status checks (`docs/ci.md`); RFCs for architectural changes | maintainers |

### CC9 — Risk mitigation

| Control | Status | Evidence | Owner |
|---|---|---|---|
| CC9.1 Identifies, selects, and develops risk-mitigation activities | implemented | Threat-model §6 ("Open risks (deliberate)") + RFC 0007 + RFC 0008 | maintainers |
| CC9.2 Vendor / business-partner risk management | partial | Dependabot rollups + license inventory in [`NOTICE`](https://github.com/zakuro-ai/zakuro/blob/master/NOTICE); formal vendor-risk register is a gap | maintainers |

### Availability — A1

| Control | Status | Evidence | Owner |
|---|---|---|---|
| A1.1 Capacity planning | partial | Worker queue-depth gauge + dispatch-latency histogram (#124) feed planning; no formal capacity plan yet | maintainers |
| A1.2 Backup, redundancy, recovery | implemented | Stateless workers + idempotent retries (RFC 0004); double-entry ledger (RFC 0005) carries the financial-correctness invariant across restarts | maintainers |
| A1.3 Recovery testing | gap | No game-day cadence yet | maintainers |

### Confidentiality — C1

| Control | Status | Evidence | Owner |
|---|---|---|---|
| C1.1 Identifies and protects confidential information | implemented | PII redactor in `zakuro.observability.sentry` (also wired into structlog #125); SOPS+age for secrets (zakuro-image#15) | maintainers |
| C1.2 Disposes of confidential information | partial | Worker drops in-memory state on shutdown; persistent ledger retention is documented in RFC 0005 but no automated purge yet | maintainers |

## ISO 27001:2022 — Annex A control mapping

Each of the 93 Annex A controls in ISO 27001:2022 is grouped into one of four themes. We claim alignment per theme:

### A.5 Organizational controls (37 controls)

Pointer rather than full table — implementation is the same set of artefacts as SOC 2:

- **A.5.1–A.5.4** (policies, roles, segregation, contact with authorities) — `CONTRIBUTING.md`, `SECURITY.md`, RFC process.
- **A.5.7** (threat intelligence) — Dependabot + osv-scanner consume upstream advisories continuously.
- **A.5.8** (information security in project management) — RFCs are required for architectural changes per `CONTRIBUTING.md`.
- **A.5.13–A.5.14** (labelling, transfer) — PII redaction policy in [`docs/security/threat-model.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/security/threat-model.md) §4 (LINDDUN); secrets-handling policy in `docs/secrets.md` (when published).
- **A.5.15–A.5.18** (access control, identity, authentication) — RFC 0002 (planned).
- **A.5.20–A.5.23** (supplier relationships) — `NOTICE` is the third-party inventory; Dependabot+SBOM cover supply-chain risk.
- **A.5.24–A.5.28** (incident management) — `SECURITY.md` disclosure policy.
- **A.5.29** (continuity) — Stateless workers + retry semantics; formal BCP is a gap.
- **A.5.30** (ICT readiness for business continuity) — gap.

### A.6 People controls (8 controls)

- **A.6.1–A.6.4** (screening, T&Cs, awareness, disciplinary) — handled at the org level outside the codebase scope. Maintainers sign GitHub's CLA-equivalent on first contribution.
- **A.6.5–A.6.7** (responsibilities on termination, confidentiality) — same.
- **A.6.8** (information security event reporting) — `SECURITY.md` `security@zakuro-ai.com` channel.

### A.7 Physical controls (14 controls)

Mostly n/a: software project, no first-party datacenter. Customers' physical posture is documented as out-of-scope in `docs/security/threat-model.md` §1.

### A.8 Technological controls (34 controls)

This is the bulk of where Zakuro's controls live.

| ISO 27001 Annex A | Status | Evidence |
|---|---|---|
| A.8.1 User endpoint devices | n/a | runtime, not endpoint software |
| A.8.2 Privileged access rights | planned | RFC 0002 (`admin:*` JWT scopes) |
| A.8.3 Information access restriction | planned | RFC 0002 + RFC 0007 (per-tenant isolation) |
| A.8.4 Access to source code | implemented | GitHub branch protection (`docs/ci.md`) |
| A.8.5 Secure authentication | planned | RFC 0002 mTLS + JWT |
| A.8.6 Capacity management | partial | Prometheus metrics (#124) |
| A.8.7 Protection against malware | implemented | Cosign signing + Rekor (zakuro-image#18); no first-party endpoints |
| A.8.8 Management of technical vulnerabilities | implemented | pip-audit + osv-scanner + trivy + Semgrep + CodeQL |
| A.8.9 Configuration management | implemented | SOPS+age (zakuro-image#15); digest-pinned base images |
| A.8.10 Information deletion | partial | Worker in-memory drop on shutdown; persistent retention policy gap |
| A.8.11 Data masking | implemented | PII redactor in `sentry.py` + reused in `logging.py` |
| A.8.12 Data leakage prevention | implemented | structlog redaction; Sentry `send_default_pii=False` |
| A.8.13 Information backup | partial | Ledger backups documented in RFC 0005; recovery testing gap |
| A.8.14 Redundancy of information processing facilities | implemented | P2P mesh (RFC 0004 + RFC 0008); peer-driven failover |
| A.8.15 Logging | implemented | structlog JSON (#125) — stable schema |
| A.8.16 Monitoring activities | implemented | Sentry + Prometheus + OTel |
| A.8.17 Clock synchronization | partial | Worker assumes NTP via host; not enforced |
| A.8.18 Use of privileged utility programs | n/a | not applicable to a Python library |
| A.8.19 Installation of software on operational systems | implemented | Distroless worker variant + dual-publish (zakuro-image#20) |
| A.8.20 Networks security | implemented | mTLS at transport (RFC 0002); QUIC over UDP only |
| A.8.21 Security of network services | implemented | mTLS + JWT (RFC 0002) |
| A.8.22 Segregation of networks | n/a | runtime, customers segregate per their topology |
| A.8.23 Web filtering | n/a | not applicable |
| A.8.24 Use of cryptography | implemented | TLS 1.3 (rustls); Ed25519 for JWT; HKDF for HMAC keys; SOPS+age for secrets |
| A.8.25 Secure development life cycle | implemented | RFC process + `CONTRIBUTING.md` review checklist |
| A.8.26 Application security requirements | implemented | Threat model + RFCs |
| A.8.27 Secure system architecture & engineering principles | implemented | RFC 0001/0002/0003/0004/0007/0008 cover the architecture |
| A.8.28 Secure coding | implemented | Required CI lanes: ruff, mypy, semgrep, CodeQL |
| A.8.29 Security testing in development and acceptance | implemented | 178+ unit + 9 integration; SAST + dependency scans on every PR |
| A.8.30 Outsourced development | n/a | no outsourcing |
| A.8.31 Separation of development, test, and production environments | partial | CI environments separate from any future hosted-broker prod; first-party prod doesn't exist yet |
| A.8.32 Change management | implemented | Required PR review |
| A.8.33 Test information | implemented | Test fixtures use synthetic data only |
| A.8.34 Protection of information systems during audit testing | n/a | no production system to protect during audit |

## Gaps to close before a real audit

In priority order, the work that would turn this doc from "alignment claim" into a defensible attestation:

1. **External pentest** ([#143](https://github.com/zakuro-ai/zakuro/issues/143)) — a third-party attestation that the implemented controls actually hold.
2. **Formal BCP / DR runbook** — recovery testing, RPO/RTO numbers, game-day cadence.
3. **Vendor risk register** — formalise the dependency licence + provenance inventory into a vendor-risk document with review cadence.
4. **Customer status page + post-mortem template** — closes the gap in CC2.3 / CC7.5.
5. **Privacy posture** — GDPR Article 17 deletion process documented, retention schedule, DPA template. Today it's hinted at in the threat-model but not operationalised.
6. **Capacity plan** — once Prometheus metrics (#124) have been live for one quarter, derive a real capacity baseline + alarm thresholds.

None of these are blockers to ship; they are the order in which a customer asking "are you SOC 2 ready?" would expect to see the project ratchet up.

## References

- [SOC 2 — TSC 2017, AICPA](https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2)
- [ISO/IEC 27001:2022 control set](https://www.iso.org/standard/27001)
- Internal: [`docs/security/threat-model.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/security/threat-model.md), [`docs/security/verifying-releases.md`](verifying-releases.md), [`docs/ci.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/ci.md)
