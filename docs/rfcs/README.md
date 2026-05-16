# Zakuro RFCs

Architecture decision records for changes that touch the public surface,
the wire protocol, or the cross-cutting infrastructure (observability,
auth, deployment). Each RFC is:

- numbered sequentially
- titled with the ticket it implements
- structured with **Context → Decision → Implementation plan → Rejected
  alternatives → Migration / rollout**
- merged before the implementation PR opens, so the implementation
  PR's diff stays focused on code, not on debating choices

The numbering is independent of GitHub issue numbers (an RFC may
implement several issues, or an issue may produce several RFCs).

| # | Title | Closes | Status |
|---|---|---|---|
| 0001 | Wire format: replace cloudpickle with postcard | #117 | Accepted |
| 0002 | Authentication: mTLS everywhere + JWT scopes | #115, #116 | Accepted (CA paragraph amended by 0004) |
| 0003 | Observability stack: hybrid Prom + OTel + structlog | #123, #124, #125 | Accepted |
| 0004 | Deployment model: P2P over QUIC, no Kubernetes | #131, #132 (won't fix) | Accepted |
| 0005 | Credit ledger: double-entry accountable | #135 | Accepted |
| 0006 | Batch dispatch: async iterator API | #137 | Accepted |
| 0007 | Tenant isolation: Docker + ephemeral single-tenant workers | #136 | Accepted |

Each RFC is **Accepted** here because the headline decision was already
made (see the question round in the May 2026 cleanup sprint). The body
of the RFC documents the implementation plan for the engineer who picks
the ticket up.
