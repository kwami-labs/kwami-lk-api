-- =============================================================================
-- Admin Invoice Reconciliation
-- Stores provider invoice imports, normalized usage lines, reconciliation runs,
-- and findings for admin-only margin auditing.
-- =============================================================================

CREATE TABLE IF NOT EXISTS provider_usage_imports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    import_mode text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    source_label text,
    invoice_period_start timestamptz,
    invoice_period_end timestamptz,
    currency text NOT NULL DEFAULT 'usd',
    external_reference text,
    imported_by text,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_usage_imports_status_check
        CHECK (status IN ('pending', 'completed', 'failed', 'partial')),
    CONSTRAINT provider_usage_imports_mode_check
        CHECK (import_mode IN ('manual', 'api_pull'))
);

CREATE INDEX IF NOT EXISTS idx_provider_usage_imports_provider_period
    ON provider_usage_imports(provider, invoice_period_start DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS provider_usage_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id uuid NOT NULL REFERENCES provider_usage_imports(id) ON DELETE CASCADE,
    provider text NOT NULL,
    service text NOT NULL,
    usage_unit text NOT NULL,
    usage_quantity double precision NOT NULL DEFAULT 0,
    raw_cost_usd double precision,
    estimated_cost_usd double precision,
    currency text NOT NULL DEFAULT 'usd',
    resource_id text,
    session_id text,
    user_id text,
    started_at timestamptz,
    ended_at timestamptz,
    external_reference text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_line jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_provider_usage_lines_import
    ON provider_usage_lines(import_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_provider_usage_lines_provider_service
    ON provider_usage_lines(provider, service, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_provider_usage_lines_session
    ON provider_usage_lines(session_id)
    WHERE session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS provider_reconciliation_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_mode text NOT NULL DEFAULT 'manual',
    provider_filters jsonb NOT NULL DEFAULT '[]'::jsonb,
    import_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    period_start timestamptz,
    period_end timestamptz,
    status text NOT NULL DEFAULT 'pending',
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by text,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_reconciliation_runs_status_check
        CHECK (status IN ('pending', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_provider_reconciliation_runs_created
    ON provider_reconciliation_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS provider_reconciliation_findings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES provider_reconciliation_runs(id) ON DELETE CASCADE,
    severity text NOT NULL,
    finding_type text NOT NULL,
    provider text,
    service text,
    session_id text,
    user_id text,
    external_reference text,
    expected_cost_usd double precision,
    actual_cost_usd double precision,
    delta_cost_usd double precision,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_reconciliation_findings_severity_check
        CHECK (severity IN ('info', 'warning', 'critical'))
);

CREATE INDEX IF NOT EXISTS idx_provider_reconciliation_findings_run
    ON provider_reconciliation_findings(run_id, severity, created_at DESC);

CREATE OR REPLACE FUNCTION set_updated_at_timestamp()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS provider_usage_imports_set_updated_at ON provider_usage_imports;
CREATE TRIGGER provider_usage_imports_set_updated_at
    BEFORE UPDATE ON provider_usage_imports
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at_timestamp();

DROP TRIGGER IF EXISTS provider_reconciliation_runs_set_updated_at ON provider_reconciliation_runs;
CREATE TRIGGER provider_reconciliation_runs_set_updated_at
    BEFORE UPDATE ON provider_reconciliation_runs
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at_timestamp();
