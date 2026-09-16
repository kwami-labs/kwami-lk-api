-- =============================================================================
-- Billing Accounting Upgrade
-- Adds richer ledger fields so provider cost, billed amount, and settlement
-- outcome are tracked separately for every usage row.
-- =============================================================================

-- Enum additions moved to 012_enum_credit_model_type.sql: ALTER TYPE ... ADD
-- VALUE cannot run inside a transaction block.

ALTER TABLE credit_usage_logs
    ADD COLUMN IF NOT EXISTS provider_cost_usd double precision,
    ADD COLUMN IF NOT EXISTS billed_cost_usd double precision,
    ADD COLUMN IF NOT EXISTS margin_usd double precision,
    ADD COLUMN IF NOT EXISTS requested_credits bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS settlement_status text NOT NULL DEFAULT 'charged',
    ADD COLUMN IF NOT EXISTS pricing_version text,
    ADD COLUMN IF NOT EXISTS pricing_source text,
    ADD COLUMN IF NOT EXISTS usage_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE credit_usage_logs
SET
    provider_cost_usd = COALESCE(provider_cost_usd, cost_usd),
    billed_cost_usd = COALESCE(billed_cost_usd, cost_usd),
    margin_usd = COALESCE(margin_usd, 0),
    requested_credits = COALESCE(requested_credits, credits_charged),
    settlement_status = COALESCE(settlement_status, 'charged'),
    pricing_version = COALESCE(pricing_version, 'legacy'),
    pricing_source = COALESCE(pricing_source, 'legacy'),
    usage_metadata = COALESCE(usage_metadata, '{}'::jsonb)
WHERE
    provider_cost_usd IS NULL
    OR billed_cost_usd IS NULL
    OR margin_usd IS NULL
    OR pricing_version IS NULL
    OR pricing_source IS NULL;

ALTER TABLE credit_usage_logs
    ALTER COLUMN provider_cost_usd SET NOT NULL,
    ALTER COLUMN billed_cost_usd SET NOT NULL,
    ALTER COLUMN margin_usd SET NOT NULL,
    ALTER COLUMN pricing_version SET NOT NULL,
    ALTER COLUMN pricing_source SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'credit_usage_logs_settlement_status_check'
    ) THEN
        ALTER TABLE credit_usage_logs
            ADD CONSTRAINT credit_usage_logs_settlement_status_check
            CHECK (settlement_status IN ('pending', 'charged', 'insufficient_credits', 'skipped'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_credit_usage_logs_settlement
    ON credit_usage_logs(user_id, settlement_status, created_at DESC);
