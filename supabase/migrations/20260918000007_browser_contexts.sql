-- =============================================================================
-- browser_contexts: remember which persisted cloud-browser profile is whose.
--
-- The agent's navigation panel runs a real cloud browser carrying the user's
-- cookies and logins, so that "open my mail" means *their* mail. Browserbase
-- calls that persisted state a Context, and it is reachable only by an opaque
-- id handed back once at creation: there is no lookup-by-name endpoint, and
-- context names are unique per project, so re-creating "kwami-<owner>" is
-- rejected rather than idempotent.
--
-- With nowhere to keep that id, every session would start a fresh context. The
-- user would be signed out of every site, and the previous context would be
-- orphaned -- still stored, still billed, and now unreachable. This table is
-- that memory.
--
-- owner_key is text and carries no foreign key on purpose. The agent keys the
-- browser on its `kwami_id`, which is the user_kwamis id for web sessions but
-- falls back to the LiveKit participant identity for telephony ones. A foreign
-- key would reject the second kind at insert time and take the browser down
-- with it. user_id is recorded when it is known, so a user can still see and
-- clear their own browsing state.
-- =============================================================================

CREATE TABLE IF NOT EXISTS browser_contexts (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_key    text NOT NULL,
    vendor       text NOT NULL CHECK (vendor IN ('browserbase', 'browser_use')),
    context_id   text NOT NULL,
    user_id      uuid REFERENCES auth.users(id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    -- One context per owner per vendor. Without this, a race between two
    -- sessions starting at once leaves two rows, and whichever is read next
    -- decides which of the user's two half-logged-in profiles they get.
    UNIQUE (owner_key, vendor)
);

CREATE INDEX IF NOT EXISTS idx_browser_contexts_user
    ON browser_contexts(user_id, updated_at DESC);

COMMENT ON TABLE browser_contexts IS
    'Maps a kwami/participant to its persisted cloud-browser profile. One row per (owner_key, vendor).';
COMMENT ON COLUMN browser_contexts.owner_key IS
    'The agent''s kwami_id: a user_kwamis id for web sessions, a LiveKit participant identity for telephony.';
COMMENT ON COLUMN browser_contexts.context_id IS
    'Vendor-side handle: a Browserbase Context id, or a Browser Use profile id.';

ALTER TABLE browser_contexts ENABLE ROW LEVEL SECURITY;

-- Writes are service-role only: the agent reaches these rows through the
-- internal API with the shared key, never as the end user. The two policies
-- below exist so a user can audit and clear their own browsing state -- which
-- is a deletion request over stored cookies, so it must not need support.
CREATE POLICY "Users can view own browser contexts"
    ON browser_contexts FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own browser contexts"
    ON browser_contexts FOR DELETE
    USING (auth.uid() = user_id);
