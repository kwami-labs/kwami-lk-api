-- +no-transaction
-- =============================================================================
-- Add 'tool' and 'memory' to credit_model_type.
--
-- Extracted from 003_billing_accounting_upgrade.sql. See the note in
-- 011_enum_channel_kind_sms.sql: ALTER TYPE ... ADD VALUE is not transactional.
-- =============================================================================

ALTER TYPE credit_model_type ADD VALUE IF NOT EXISTS 'tool';
ALTER TYPE credit_model_type ADD VALUE IF NOT EXISTS 'memory';
