-- =============================================================================
-- livekit_sessions: bind every issued LiveKit room to the user it was issued to.
--
-- POST /token accepted `roomName` and `kwamiId` straight from the request body
-- with no ownership check at all, while every other domain validated against
-- user_kwamis. Any authenticated user could therefore mint a token for another
-- tenant's room -- joining a live conversation -- and dispatch an agent carrying
-- someone else's kwami_id.
--
-- Recording the room at issue time makes room ownership first-claim-wins, and
-- gives the usage-report path a trustworthy way to decide whose balance to
-- charge instead of believing the user_id the agent sends.
-- =============================================================================

CREATE TABLE IF NOT EXISTS livekit_sessions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    room_name    text NOT NULL UNIQUE,
    user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id     uuid REFERENCES user_kwamis(id) ON DELETE SET NULL,
    source       text NOT NULL DEFAULT 'web'
                 CHECK (source IN ('web', 'sip_inbound', 'sip_outbound')),
    status       text NOT NULL DEFAULT 'issued'
                 CHECK (status IN ('issued', 'active', 'ended', 'abandoned')),
    -- Reserved for the mid-session credit hold: /token currently checks the
    -- balance once, so N parallel sessions all pass the same check.
    hold_micro   bigint NOT NULL DEFAULT 0 CHECK (hold_micro >= 0),
    issued_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    ended_at     timestamptz
);

CREATE INDEX IF NOT EXISTS idx_livekit_sessions_user_active
    ON livekit_sessions(user_id, status)
    WHERE status IN ('issued', 'active');

CREATE INDEX IF NOT EXISTS idx_livekit_sessions_kwami
    ON livekit_sessions(kwami_id, issued_at DESC);

COMMENT ON TABLE livekit_sessions IS
    'One row per issued LiveKit room. room_name is UNIQUE, so a room belongs to the first user it was issued to.';

ALTER TABLE livekit_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own sessions"
    ON livekit_sessions FOR SELECT
    USING (auth.uid() = user_id);
