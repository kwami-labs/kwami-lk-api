-- =============================================================================
-- Corrective migration for 009_kwami_contacts_fields.sql
--
-- 009 carried a stray `;` after `notes text`, which terminated the ALTER TABLE.
-- The statement therefore never created `instagram` or `tiktok`, while
-- services/channels.py (create_contact, contact search) and routes/contacts.py
-- (PATCH) write and filter on both -- so contact create, update and search have
-- been returning 500 in production.
--
-- 009 is repaired in place as well, because the broken statement provably never
-- executed past line 5, so the repaired file is correct for a fresh database and
-- a no-op difference for an existing one. This file exists because production has
-- already recorded 009 as run and nobody re-runs applied files.
-- =============================================================================

ALTER TABLE kwami_contacts
    ADD COLUMN IF NOT EXISTS email text,
    ADD COLUMN IF NOT EXISTS notes text,
    ADD COLUMN IF NOT EXISTS instagram text,
    ADD COLUMN IF NOT EXISTS tiktok text;
