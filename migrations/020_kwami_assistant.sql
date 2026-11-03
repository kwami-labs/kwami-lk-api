-- =============================================================================
-- Kwami assistant
-- What outlives a voice session: reminders, things being followed up, scheduled
-- reports and the notifications they produce, the link to the user's own Google
-- account, and the record of every message sent without the app's dialog.
-- =============================================================================

CREATE TABLE kwami_assistant_settings (
    kwami_id        uuid PRIMARY KEY REFERENCES user_kwamis(id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    notify_to       text,
    notify_channel  text NOT NULL DEFAULT 'whatsapp' CHECK (notify_channel IN ('whatsapp', 'sms')),
    timezone        text NOT NULL DEFAULT 'Europe/Madrid',
    reports_enabled boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kwami_assistant_tasks (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id     uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('reminder', 'followup', 'report')),
    title        text NOT NULL,
    notes        text NOT NULL DEFAULT '',
    recurrence   text NOT NULL DEFAULT 'none' CHECK (recurrence IN ('none', 'once', 'daily', 'weekly')),
    weekdays     smallint[] NOT NULL DEFAULT '{}',
    local_time   text,
    timezone     text NOT NULL DEFAULT 'Europe/Madrid',
    report_type  text CHECK (report_type IN ('monday', 'friday', 'sunday')),
    next_run_at  timestamptz,
    last_run_at  timestamptz,
    status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_kwami_assistant_tasks_due
    ON kwami_assistant_tasks(next_run_at) WHERE status = 'open' AND next_run_at IS NOT NULL;
CREATE INDEX idx_kwami_assistant_tasks_kwami ON kwami_assistant_tasks(user_id, kwami_id, status);
-- Not partial: PostgREST's on_conflict cannot name a partial index. NULLs stay distinct,
-- so any number of reminders share a kwami and only reports collide.
CREATE UNIQUE INDEX uq_kwami_assistant_report ON kwami_assistant_tasks(kwami_id, report_type);

CREATE TABLE kwami_assistant_notifications (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id       uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    task_id        uuid REFERENCES kwami_assistant_tasks(id) ON DELETE SET NULL,
    title          text NOT NULL,
    body           text NOT NULL,
    delivered_via  text NOT NULL DEFAULT 'app',
    delivery_error text,
    read_at        timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_kwami_assistant_notifications_kwami
    ON kwami_assistant_notifications(user_id, kwami_id, created_at DESC);

-- Tokens: no policy on purpose. Only the service role reads this table, and the
-- refresh token inside it is encrypted by the API before it arrives.
CREATE TABLE kwami_google_accounts (
    user_id                 uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email                   text NOT NULL,
    refresh_token_encrypted text NOT NULL,
    scopes                  text NOT NULL DEFAULT '',
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kwami_outbound_audit (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id      uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    channel       text NOT NULL,
    recipient     text NOT NULL,
    summary       text NOT NULL DEFAULT '',
    express_order text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE kwami_assistant_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_assistant_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_assistant_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_google_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_outbound_audit ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON kwami_google_accounts FROM anon, authenticated;

CREATE POLICY "Users can view own assistant settings"
    ON kwami_assistant_settings FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can view own assistant tasks"
    ON kwami_assistant_tasks FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can view own assistant notifications"
    ON kwami_assistant_notifications FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can view own outbound audit"
    ON kwami_outbound_audit FOR SELECT USING (auth.uid() = user_id);

CREATE TRIGGER kwami_assistant_settings_updated_at
    BEFORE UPDATE ON kwami_assistant_settings
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();
CREATE TRIGGER kwami_assistant_tasks_updated_at
    BEFORE UPDATE ON kwami_assistant_tasks
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();
CREATE TRIGGER kwami_google_accounts_updated_at
    BEFORE UPDATE ON kwami_google_accounts
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();
