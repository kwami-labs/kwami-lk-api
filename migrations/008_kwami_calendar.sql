-- =============================================================================
-- Kwami Calendar
-- Per-kwami calendar events for app UI and agent-managed scheduling.
-- =============================================================================

CREATE TYPE kwami_calendar_event_type AS ENUM (
    'meeting',
    'task',
    'personal',
    'reminder',
    'focus',
    'other'
);

CREATE TABLE kwami_calendar_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id        uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    title           text NOT NULL,
    description     text NOT NULL DEFAULT '',
    starts_at       timestamptz NOT NULL,
    ends_at         timestamptz NOT NULL,
    all_day         boolean NOT NULL DEFAULT false,
    event_type      kwami_calendar_event_type NOT NULL DEFAULT 'other',
    color           text NOT NULL DEFAULT '#6366f1',
    location        text NOT NULL DEFAULT '',
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kwami_calendar_events_time_range CHECK (ends_at >= starts_at)
);

CREATE INDEX idx_kwami_calendar_events_user_kwami_start
    ON kwami_calendar_events(user_id, kwami_id, starts_at);
CREATE INDEX idx_kwami_calendar_events_user_kwami_end
    ON kwami_calendar_events(user_id, kwami_id, ends_at);

COMMENT ON TABLE kwami_calendar_events IS 'Calendar events scoped to a specific kwami.';

ALTER TABLE kwami_calendar_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own calendar events"
    ON kwami_calendar_events FOR SELECT
    USING (auth.uid() = user_id);

CREATE TRIGGER kwami_calendar_events_updated_at
    BEFORE UPDATE ON kwami_calendar_events
    FOR EACH ROW
    EXECUTE PROCEDURE set_kwami_communications_updated_at();
