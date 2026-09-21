# `supabase/migrations/` is not this service's schema

**The schema this API runs against lives in [`../../migrations/`](../../migrations/)**,
applied by `scripts/migrate.py`, checksum-verified in `schema_migrations`, and
covered by the `migrations` and `integration` CI jobs.

These files are a *different, earlier* schema. They were described elsewhere in
the docs as a "hosted-project mirror", which they are not — the two sets share
exactly two tables (`browser_contexts`, `livekit_sessions`) out of 37:

| | `migrations/` | `supabase/migrations/` |
|---|---|---|
| files | 20 | 7 |
| tables | 29 | 12 |
| only here | `user_credits`, `credit_transactions`, `user_kwamis`, every `kwami_*` and `wallet_*` table | `account_energy`, `energy_ledger`, `game_sessions`, `kwami_programs`, `kwamis`, `kwami_secrets`, `profiles`, `transcript_turns`, `valuations`, `wallet_identities` |

Note `kwamis` here versus `user_kwamis` in the live set: the code reads the latter.

## Do not run `supabase db push`

It applies *this* directory. A database provisioned from it cannot serve this
API — none of the credits, kwami or wallet tables exist.

`tests/integration/contracts/test_schema_source_of_truth.py` fails if a table
the code depends on appears only here, so the two cannot be confused again.

## What to do with these files

Keep them only as history for the earlier product, or delete the directory. That
is a product decision, not a technical one, which is why this README exists
instead of a deletion.
