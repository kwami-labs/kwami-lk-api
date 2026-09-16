-- =============================================================================
-- Kwami Email (Smart Hub)
-- Per-kwami email accounts and inbound/outbound message storage with
-- automatic categorisation for Action Card rendering.
-- =============================================================================

CREATE TYPE kwami_email_category AS ENUM (
    'travel',
    'bills',
    'events',
    'newsletters',
    'personal',
    'notifications',
    'shopping',
    'work',
    'uncategorized'
);

-- One email identity per kwami (username@kwami.io).
CREATE TABLE kwami_email_accounts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id        uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    username        text NOT NULL,
    email_address   text GENERATED ALWAYS AS (username || '@kwami.io') STORED,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_kwami_email_accounts_username ON kwami_email_accounts(username);
CREATE UNIQUE INDEX idx_kwami_email_accounts_user_kwami
    ON kwami_email_accounts(user_id, kwami_id);

COMMENT ON TABLE kwami_email_accounts IS 'One email identity (username@kwami.io) per kwami.';

-- All inbound and outbound email messages.
CREATE TABLE kwami_email_messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL REFERENCES kwami_email_accounts(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id            uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    direction           kwami_event_direction NOT NULL,  -- reuse existing enum
    from_address        text NOT NULL,
    to_addresses        jsonb NOT NULL DEFAULT '[]'::jsonb,
    cc_addresses        jsonb NOT NULL DEFAULT '[]'::jsonb,
    subject             text NOT NULL DEFAULT '',
    body_text           text NOT NULL DEFAULT '',
    body_html           text NOT NULL DEFAULT '',
    headers             jsonb NOT NULL DEFAULT '{}'::jsonb,
    sendgrid_message_id text,
    category            kwami_email_category NOT NULL DEFAULT 'uncategorized',
    action_card_data    jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_read             boolean NOT NULL DEFAULT false,
    is_archived         boolean NOT NULL DEFAULT false,
    is_starred          boolean NOT NULL DEFAULT false,
    received_at         timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_kwami_email_messages_inbox
    ON kwami_email_messages(user_id, kwami_id, is_archived, received_at DESC);
CREATE INDEX idx_kwami_email_messages_category
    ON kwami_email_messages(user_id, kwami_id, category, received_at DESC);
CREATE UNIQUE INDEX idx_kwami_email_messages_sendgrid_id
    ON kwami_email_messages(sendgrid_message_id)
    WHERE sendgrid_message_id IS NOT NULL;

-- RLS
ALTER TABLE kwami_email_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_email_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own email accounts"
    ON kwami_email_accounts FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can view own email messages"
    ON kwami_email_messages FOR SELECT
    USING (auth.uid() = user_id);

-- updated_at triggers (reuse the function from 004)
CREATE TRIGGER kwami_email_accounts_updated_at
    BEFORE UPDATE ON kwami_email_accounts
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();

CREATE TRIGGER kwami_email_messages_updated_at
    BEFORE UPDATE ON kwami_email_messages
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();
