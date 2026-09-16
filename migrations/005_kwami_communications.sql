-- =============================================================================
-- Kwami Communications
-- Per-kwami channels, contacts, conversations, and event logs for telephony
-- and WhatsApp messaging.
-- =============================================================================

CREATE TYPE kwami_channel_kind AS ENUM ('voice_phone', 'whatsapp');
CREATE TYPE kwami_conversation_kind AS ENUM ('call', 'whatsapp');
CREATE TYPE kwami_event_direction AS ENUM ('inbound', 'outbound');

CREATE TABLE kwami_channels (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                    uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    kind                        kwami_channel_kind NOT NULL,
    provider                    text NOT NULL DEFAULT 'twilio',
    status                      text NOT NULL DEFAULT 'pending',
    phone_number                text NOT NULL,
    display_name                text,
    country_code                text NOT NULL DEFAULT 'US',
    capabilities                jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_channel_sid        text,
    provider_subresource_sid    text,
    provider_sender             text,
    livekit_outbound_trunk_id   text,
    metadata                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_kwami_channels_user_kwami ON kwami_channels(user_id, kwami_id, kind);
CREATE UNIQUE INDEX idx_kwami_channels_provider_unique
    ON kwami_channels(provider, kind, phone_number);

COMMENT ON TABLE kwami_channels IS 'Provider-backed voice and WhatsApp channels owned by a specific kwami.';

CREATE TABLE kwami_contacts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id            uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    display_name        text,
    phone_number        text NOT NULL,
    whatsapp_address    text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_kwami_contacts_unique_phone
    ON kwami_contacts(user_id, kwami_id, phone_number);

CREATE TABLE kwami_conversations (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    channel_id              uuid NOT NULL REFERENCES kwami_channels(id) ON DELETE CASCADE,
    contact_id              uuid REFERENCES kwami_contacts(id) ON DELETE SET NULL,
    kind                    kwami_conversation_kind NOT NULL,
    status                  text NOT NULL DEFAULT 'active',
    external_thread_id      text,
    last_inbound_at         timestamptz,
    last_outbound_at        timestamptz,
    metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_kwami_conversations_user_kwami
    ON kwami_conversations(user_id, kwami_id, updated_at DESC);

CREATE TABLE kwami_call_events (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id         uuid REFERENCES kwami_conversations(id) ON DELETE SET NULL,
    channel_id              uuid NOT NULL REFERENCES kwami_channels(id) ON DELETE CASCADE,
    user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    direction               kwami_event_direction NOT NULL,
    provider_call_sid       text,
    livekit_room_name       text,
    participant_identity    text,
    from_number             text,
    to_number               text,
    status                  text NOT NULL DEFAULT 'queued',
    duration_seconds        integer,
    error_code              text,
    error_message           text,
    provider_payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_kwami_call_events_user_kwami
    ON kwami_call_events(user_id, kwami_id, created_at DESC);
CREATE UNIQUE INDEX idx_kwami_call_events_provider_sid
    ON kwami_call_events(provider_call_sid)
    WHERE provider_call_sid IS NOT NULL;

CREATE TABLE kwami_message_events (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id         uuid REFERENCES kwami_conversations(id) ON DELETE SET NULL,
    channel_id              uuid NOT NULL REFERENCES kwami_channels(id) ON DELETE CASCADE,
    contact_id              uuid REFERENCES kwami_contacts(id) ON DELETE SET NULL,
    user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    direction               kwami_event_direction NOT NULL,
    provider_message_sid    text,
    provider_status         text,
    from_address            text,
    to_address              text,
    body                    text,
    error_code              text,
    error_message           text,
    requires_followup       boolean NOT NULL DEFAULT false,
    provider_payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_kwami_message_events_user_kwami
    ON kwami_message_events(user_id, kwami_id, created_at DESC);
CREATE UNIQUE INDEX idx_kwami_message_events_provider_sid
    ON kwami_message_events(provider_message_sid)
    WHERE provider_message_sid IS NOT NULL;

ALTER TABLE kwami_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_call_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_message_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own kwami channels"
    ON kwami_channels FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can view own kwami contacts"
    ON kwami_contacts FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can view own kwami conversations"
    ON kwami_conversations FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can view own kwami call events"
    ON kwami_call_events FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can view own kwami message events"
    ON kwami_message_events FOR SELECT
    USING (auth.uid() = user_id);

CREATE OR REPLACE FUNCTION set_kwami_communications_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER kwami_channels_updated_at
    BEFORE UPDATE ON kwami_channels
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();

CREATE TRIGGER kwami_contacts_updated_at
    BEFORE UPDATE ON kwami_contacts
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();

CREATE TRIGGER kwami_conversations_updated_at
    BEFORE UPDATE ON kwami_conversations
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();

CREATE TRIGGER kwami_call_events_updated_at
    BEFORE UPDATE ON kwami_call_events
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();

CREATE TRIGGER kwami_message_events_updated_at
    BEFORE UPDATE ON kwami_message_events
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();

