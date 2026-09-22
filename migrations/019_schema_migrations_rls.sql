-- =============================================================================
-- Row level security on the migration registry.
--
-- 000_migrations_registry.sql creates `schema_migrations` and stops there. Every
-- other table in this schema has RLS enabled -- 014 did exactly this for the four
-- reconciliation tables, with the same reasoning: Supabase grants `anon` and
-- `authenticated` access to new tables in `public` by default, so anything created
-- without an explicit policy is readable with the public anon key.
--
-- What leaks here is not user data. It is the list of every migration that has run,
-- with names like `010_kwami_wallets` and `017_payment_event_idempotency` -- a map
-- of the schema, handed to an unauthenticated caller. That is reconnaissance, and
-- there is no reason for any role but the service role to see it.
--
-- No policies are created. Only `scripts/migrate.py` touches this table, and it
-- connects with the Postgres superuser over a direct DSN rather than through
-- PostgREST, so RLS does not apply to it. RLS enabled with zero policies is the
-- correct deny-all for every other role.
-- =============================================================================

ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON schema_migrations FROM anon, authenticated;

COMMENT ON TABLE schema_migrations IS
    'One row per applied migration file. Managed by scripts/migrate.py. '
    'RLS-denied to every role but the service role: the file list is a schema map.';
