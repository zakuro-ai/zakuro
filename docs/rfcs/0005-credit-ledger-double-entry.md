# RFC 0005 — Credit ledger: double-entry accountable

- **Status:** Accepted (2026-05)
- **Closes:** [#135](https://github.com/zakuro-ai/zakuro/issues/135)
- **Depends on:** RFC 0004 (deployment substrate), RFC 0002 (auth: tenant identity comes from JWT claims)

## Context

The runtime tracks credits per tenant — every dispatch costs CPU/memory/GPU-seconds at a per-worker price, and the broker collects from a tenant's wallet. Today the ledger is a single-entry Postgres table (`credits_balance`) updated in place; that's auditable only in the trivial sense and impossible to reconcile after a partition or a crash.

The user picked **double-entry** over single-entry+audit-log in the May 2026 question round. Reasons:

- Every credit movement is intrinsically a *transfer* between two accounts (tenant wallet → revenue account, escrow → tenant on refund). Modelling that as two debit/credit rows lets us preserve the conservation invariant `SUM(amount) per transaction_id == 0` in the schema itself.
- Catches accounting bugs at write time rather than at reconciliation time.
- Aligns with what auditors expect for the SOC 2 compliance work (#139).

## Decision

**Adopt a classical double-entry ledger.** Each credit movement is one `transaction` row plus two-or-more `entry` rows whose amounts sum to zero. Balances are derived from the entries, never updated in place; a cached materialised view refreshes hourly for fast UI reads.

The conservation invariant is enforced by:

1. A `CHECK` constraint at the SQL level on the transaction-level entries.
2. An after-insert trigger that rolls back the transaction if any entry violates `SUM(amount) WHERE transaction_id = ... = 0`.
3. A periodic reconciliation job (`scripts/reconcile_ledger.py`) that asserts the invariant globally and emits a Sentry event on drift.

## Schema

```sql
CREATE SCHEMA IF NOT EXISTS ledger;

CREATE TYPE ledger.account_kind AS ENUM (
    'tenant_wallet',     -- tenants pay from here
    'tenant_escrow',     -- pre-authorised but not yet captured
    'mesh_revenue',      -- mesh-level revenue
    'worker_payout',     -- per-worker accruals (provider-side payouts)
    'fee_platform',      -- platform fee retention
    'refund_clearing'    -- transient bucket for refunds
);

CREATE TABLE ledger.accounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NULL,                                 -- NULL for mesh-level
    worker_id   UUID NULL,                                 -- NULL except for worker_payout
    kind        ledger.account_kind NOT NULL,
    currency    CHAR(3) NOT NULL DEFAULT 'USD',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, worker_id, kind, currency)
);

CREATE TYPE ledger.transaction_kind AS ENUM (
    'dispatch_charge',   -- tenant pays for a successful dispatch
    'dispatch_refund',   -- worker failed, tenant credited back
    'topup',             -- tenant added funds
    'payout',            -- worker withdrew earnings
    'platform_fee',      -- platform retention
    'adjustment'         -- manual reconciliation
);

CREATE TABLE ledger.transactions (
    id            UUID PRIMARY KEY,                        -- caller-provided for idempotency
    kind          ledger.transaction_kind NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at     TIMESTAMPTZ NULL                         -- when the conservation check passed
);

CREATE TABLE ledger.entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID NOT NULL REFERENCES ledger.transactions(id) ON DELETE RESTRICT,
    account_id      UUID NOT NULL REFERENCES ledger.accounts(id) ON DELETE RESTRICT,
    direction       CHAR(1) NOT NULL CHECK (direction IN ('D', 'C')),  -- Debit / Credit
    amount_micros   BIGINT NOT NULL CHECK (amount_micros > 0),          -- integer micros, never fractional
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Conservation invariant: signed amount sums to zero per transaction.
-- (Debits are positive, credits are negative; sums to zero across the
--  entries of a single transaction.)
CREATE OR REPLACE FUNCTION ledger.assert_balanced() RETURNS trigger AS $$
DECLARE
    s BIGINT;
BEGIN
    SELECT SUM(CASE direction WHEN 'D' THEN amount_micros ELSE -amount_micros END)
      INTO s
      FROM ledger.entries
     WHERE transaction_id = NEW.transaction_id;
    IF s != 0 THEN
        RAISE EXCEPTION 'ledger imbalance: txn % sum=%', NEW.transaction_id, s;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER assert_balanced_after
    AFTER INSERT OR UPDATE ON ledger.entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger.assert_balanced();

-- Append-only safety: no UPDATE or DELETE on posted entries.
REVOKE UPDATE, DELETE ON ledger.entries  FROM PUBLIC;
REVOKE UPDATE, DELETE ON ledger.transactions FROM PUBLIC;
```

### Balance projection

Reads ask the materialised view, which is refreshed concurrently every minute:

```sql
CREATE MATERIALIZED VIEW ledger.account_balances AS
SELECT
    a.id          AS account_id,
    a.tenant_id,
    a.worker_id,
    a.kind,
    a.currency,
    COALESCE(SUM(
        CASE e.direction WHEN 'D' THEN e.amount_micros ELSE -e.amount_micros END
    ), 0) AS balance_micros
FROM ledger.accounts a
LEFT JOIN ledger.entries e ON e.account_id = a.id
GROUP BY a.id;

CREATE UNIQUE INDEX ON ledger.account_balances (account_id);

-- Refreshed by the broker every 60 s:
REFRESH MATERIALIZED VIEW CONCURRENTLY ledger.account_balances;
```

A tenant's wallet read path:

```python
balance = db.fetch_one(
    "SELECT balance_micros FROM ledger.account_balances "
    "WHERE tenant_id = $1 AND kind = 'tenant_wallet'",
    tenant_id,
)
```

For exact reads (post-charge, pre-confirm), the broker falls back to scanning entries directly.

## Sample journals

Charge a tenant 1.50 USD for a dispatch on worker-7:

```text
txn  = T1234, kind=dispatch_charge
entry: account=tenant-acme-wallet,   direction=C, amount=1_500_000  (credit = decrease)
entry: account=worker-7-payout,      direction=D, amount=1_425_000  (debit  = increase)
entry: account=platform-fee,         direction=D, amount=75_000     (5% platform fee)
SUM(D)=1_500_000  SUM(C)=1_500_000   net=0   ✓
```

Refund the same dispatch later:

```text
txn  = T1234R, kind=dispatch_refund, metadata={original_txn: T1234}
entry: account=tenant-acme-wallet,   direction=D, amount=1_500_000
entry: account=worker-7-payout,      direction=C, amount=1_425_000
entry: account=platform-fee,         direction=C, amount=75_000
```

Idempotency: a `POST /ledger/charge` carries a caller-supplied `transaction_id`. A duplicate `INSERT INTO ledger.transactions` collides on the primary key and returns the existing rows unchanged.

## Implementation plan

1. **Schema migration** lands as `migrations/0001_ledger_double_entry.sql`. Applied at broker startup; idempotent (`CREATE ... IF NOT EXISTS`).
2. **Python API** in `zakuro/ledger/`:
   - `charge(tenant_id, worker_id, micros, *, transaction_id)` — opens a Postgres tx, writes 3 entries, asserts conservation.
   - `refund(transaction_id)` — looks up the original entries, writes inverses.
   - `balance(tenant_id)` — reads the materialised view (or scans entries with `?exact=true`).
3. **Backfill** (one-shot script): convert the existing `credits_balance` column into `accounts` + a single `topup` transaction per tenant. After backfill, drop the old table.
4. **Reconciliation job** runs hourly and emits:
   - Sentry event on any conservation drift
   - Prometheus counter `zakuro_ledger_imbalance_total{kind=...}`
5. **API surface** documented in `docs/STABILITY.md` under "Ledger API (v0.5)".

## Rejected alternatives

| Option | Why rejected |
|---|---|
| Single-entry + append-only audit log | Auditable but doesn't enforce conservation at write time. SOC 2 reviewer specifically asked for double-entry. |
| Event-sourced (Kafka log + projection) | Powerful but adds a Kafka dep on a P2P deployment. Conservation invariants are harder to express in stream-processor land. |
| Stripe-style "balance transactions" via an external API | Pushes the source-of-truth out of process — cross-mesh reconciliation now depends on Stripe's availability. Out of scope. |

## Open questions for implementation time

- **Currency.** Single-currency (USD) at first. Multi-currency requires per-currency conservation checks and FX-rate sourcing — defer.
- **Precision.** Micros (`10⁻⁶`) at the storage layer is enough for sub-cent dispatch costs without floating-point drift. If pricing ever goes below a micro, switch to picos (`10⁻¹²`).
- **Retention.** Append-only means the table grows monotonically. Plan: keep all entries forever, partition `ledger.entries` by month, archive partitions older than 24 months to S3-compatible cold storage. Track separately when the bytes warrant it.
- **Snapshotting.** A monthly snapshot of `account_balances` lets the materialised view refresh remain cheap as the entries table grows. Implement in the reconciliation job.
