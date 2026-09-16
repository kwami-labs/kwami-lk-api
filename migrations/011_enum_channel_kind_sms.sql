-- +no-transaction
-- =============================================================================
-- Add 'sms' to kwami_channel_kind.
--
-- Extracted from 005_kwami_communications.sql and 009_kwami_contacts_fields.sql,
-- which each carried a copy. ALTER TYPE ... ADD VALUE cannot run inside a
-- transaction block, so this file must be applied with autocommit -- hence the
-- `-- +no-transaction` directive read by scripts/migrate.py.
--
-- The new value is not usable until this statement commits, so any migration or
-- code that stores 'sms' must come strictly after this file.
-- =============================================================================

ALTER TYPE kwami_channel_kind ADD VALUE IF NOT EXISTS 'sms';
