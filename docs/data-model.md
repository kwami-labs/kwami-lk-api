# Data model

Postgres is the system of record. The schema lives in `migrations/` as
numbered SQL files. A parallel history exists under `supabase/migrations/`
for the hosted project; `scripts/migrate.py` applies `migrations/` and is
what CI and `make migrate` run.

Connection comes from `DATABASE_URL`. Files run in numeric-prefix order
inside a transaction. A file whose first line is `-- +no-transaction`
runs with autocommit — `ALTER TYPE ... ADD VALUE` cannot execute inside
a transaction block.

## Core entities

```mermaid
erDiagram
  auth_users ||--o| user_credits : "balance"
  auth_users ||--o{ user_kwamis : owns
  auth_users ||--o{ credit_transactions : ledger
  auth_users ||--o{ credit_usage_logs : usage
  auth_users ||--o{ livekit_sessions : rooms
  user_kwamis ||--o{ kwami_channels : addresses
  user_kwamis ||--o{ kwami_contacts : people
  user_kwamis ||--o{ kwami_wallets : custody
  user_kwamis ||--o{ calendar_events : events
  user_kwamis ||--o| email_accounts : inbox
  livekit_sessions ||--o{ credit_usage_logs : "session_id = room_name"
  payment_events ||--o| credit_transactions : "idempotency"
  usage_reports ||--o{ credit_usage_logs : settlement

  user_credits {
    uuid user_id PK
    bigint balance
    bigint lifetime_purchased
    bigint lifetime_used
  }

  user_kwamis {
    uuid id PK
    uuid user_id
    text name
    jsonb config
  }

  livekit_sessions {
    uuid id PK
    text room_name UK
    uuid user_id
    uuid kwami_id
    text source
    text status
    bigint hold_micro
  }

  credit_transactions {
    uuid id PK
    uuid user_id
    enum type
    bigint amount
    bigint balance_after
    text idempotency_key
  }

  credit_usage_logs {
    uuid id PK
    uuid user_id
    text session_id
    enum model_type
    text settlement_status
    float provider_cost_usd
    float billed_cost_usd
  }
```

`auth.users` is Supabase Auth. Application tables reference it with
`ON DELETE CASCADE`.

## Migration log

| Version | File | What it introduced |
|---------|------|--------------------|
| 000 | `migrations_registry` | `schema_migrations` + checksums |
| 001 | `credits_system` | balances, ledger, usage logs, `add_credits` / `deduct_credits`, RLS |
| 002 | `user_kwamis` | per-user agent workspaces |
| 003 | `billing_accounting_upgrade` | provider / billed / margin columns, settlement status |
| 004 | `admin_invoice_reconciliation` | vendor import + findings |
| 005 | `kwami_communications` | channels, conversations, call/message events |
| 006 | `user_app_settings` | client preferences |
| 007 | `kwami_email` | Smart Hub accounts and messages |
| 008 | `kwami_calendar` | per-kwami events |
| 009 | `kwami_contacts_fields` | contact metadata |
| 010 | `kwami_wallets` | custody wallets, allowlist, funding |
| 011 | `enum_channel_kind_sms` | SMS on the channel enum |
| 012 | `enum_credit_model_type` | `tool` / `memory` usage types |
| 013 | `fix_contacts_fields` | contact column repair |
| 014 | `rls_reconciliation` | RLS on reconciliation tables |
| 015 | `livekit_sessions` | room ownership |
| 016 | `wallet_funding_repair` | funding idempotency repair |
| 017 | `payment_event_idempotency` | `payment_events` unique `(provider, event_id)` |

Do not edit an applied file. `make migrate-verify` checksums the bytes
against `schema_migrations`. A change in place is a new migration.

`--baseline 010` exists for databases that were migrated by hand before
the registry. It records history without re-running those files.

## Invariants that live in SQL

These are the reason the integration lane exists.

**Balance cannot go negative.** `deduct_credits` updates only when
`balance >= p_amount`. The `WHERE` and the write are one statement.

**A payment credits once.** `credit_transactions.idempotency_key` is
unique where set. `add_credits` inserts the ledger row with
`ON CONFLICT DO NOTHING` and returns the current balance.

**A room has one owner.** `livekit_sessions.room_name` is `UNIQUE`. The
first insert wins; a concurrent insert re-reads and 403s if the owner
differs.

**A webhook is processed once.** `payment_events (provider, event_id)`
is unique. `usage_reports.report_key` is unique.

**Tenants cannot read each other.** Every user-facing table has RLS
enabled. Policies are `auth.uid() = user_id` (or a join through
`user_kwamis`). The API's service role bypasses this; the policies
protect direct PostgREST access and anything that forgets the service
role.

## Dual migration trees

`supabase/migrations/` is what `supabase db push` / hosted previews
apply. `migrations/` is what this service's runner applies. They must
describe the same schema. When you add a table:

1. Add `migrations/NNN_name.sql` with the table, indexes, RLS, grants.
2. Mirror it under `supabase/migrations/YYYYMMDDHHMMSS_name.sql` if the
   hosted project is the one production reads.
3. Add or raise a coverage floor if you added Python that talks to it.
4. Add an integration test if the file contains an RPC, a unique
   index, or a policy.

CI's `migrations` job refuses duplicate numeric prefixes and an
unorderable set. It does not diff the two trees — that is a review
check.

## Applying locally

```bash
# throwaway Postgres used by the integration lane
make test-db-up

# list what would run, refuse duplicate prefixes
make migrate-dry-run

# apply (needs DATABASE_URL)
DATABASE_URL=postgresql://postgres:test@localhost:55433/kwami_test make migrate
```

The integration tests apply the files themselves through the same
runner. You do not need to migrate by hand to run `make test-integration`.
