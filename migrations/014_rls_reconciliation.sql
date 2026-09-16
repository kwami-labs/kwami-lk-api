-- =============================================================================
-- Enable RLS on the provider reconciliation tables.
--
-- 004_admin_invoice_reconciliation.sql created four tables and never enabled row
-- level security on any of them. Supabase grants the `anon` and `authenticated`
-- roles access to new tables in the `public` schema by default, so raw provider
-- invoices, per-user costs and margin data have been readable with the public
-- anon key.
--
-- No policies are created: these are service-role-only tables (the API reads them
-- through the secret key, which bypasses RLS). RLS enabled with zero policies is
-- the correct deny-all for every other role.
-- =============================================================================

ALTER TABLE provider_usage_imports           ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_usage_lines             ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_reconciliation_runs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_reconciliation_findings ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON provider_usage_imports           FROM anon, authenticated;
REVOKE ALL ON provider_usage_lines             FROM anon, authenticated;
REVOKE ALL ON provider_reconciliation_runs     FROM anon, authenticated;
REVOKE ALL ON provider_reconciliation_findings FROM anon, authenticated;

-- 004 stores these as `text` rather than a uuid FK, so nothing stops malformed
-- values accumulating in the audit trail. Constrain the shape now; the type
-- change is a later, heavier migration.
ALTER TABLE provider_usage_lines
    DROP CONSTRAINT IF EXISTS provider_usage_lines_user_id_shape;
ALTER TABLE provider_usage_lines
    ADD CONSTRAINT provider_usage_lines_user_id_shape
    CHECK (user_id IS NULL OR user_id ~ '^[0-9a-fA-F-]{36}$') NOT VALID;

ALTER TABLE provider_reconciliation_findings
    DROP CONSTRAINT IF EXISTS provider_reconciliation_findings_user_id_shape;
ALTER TABLE provider_reconciliation_findings
    ADD CONSTRAINT provider_reconciliation_findings_user_id_shape
    CHECK (user_id IS NULL OR user_id ~ '^[0-9a-fA-F-]{36}$') NOT VALID;
