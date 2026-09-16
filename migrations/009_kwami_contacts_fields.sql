-- Add richer contact profile fields for Contacts app UX.

ALTER TABLE kwami_contacts
    ADD COLUMN IF NOT EXISTS email text,
    ADD COLUMN IF NOT EXISTS notes text,
    ADD COLUMN IF NOT EXISTS instagram text,
    ADD COLUMN IF NOT EXISTS tiktok text;

CREATE INDEX IF NOT EXISTS idx_kwami_contacts_user_kwami_updated
    ON kwami_contacts(user_id, kwami_id, updated_at DESC);

