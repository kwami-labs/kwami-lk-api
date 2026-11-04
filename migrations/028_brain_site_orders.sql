-- One more order for the PC that needs no model: a site in Jarvis's Chrome, for the
-- user to sign in to from the app. brain_claim_job hands it to the quick lane too.

ALTER TABLE brain_jobs DROP CONSTRAINT IF EXISTS brain_jobs_mode_check;
ALTER TABLE brain_jobs ADD CONSTRAINT brain_jobs_mode_check
    CHECK (mode IN ('work', 'think', 'research', 'merge', 'login', 'site'));

-- The sites the user signed in to, a few at a time, and how each session stands.
CREATE TABLE brain_sites (
    user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id   uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    host       text NOT NULL,
    url        text NOT NULL,
    state      text NOT NULL DEFAULT 'pending'
               CHECK (state IN ('pending', 'connected', 'signed_out', 'refused')),
    checked_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, host)
);

ALTER TABLE brain_sites ENABLE ROW LEVEL SECURITY;
REVOKE INSERT, UPDATE, DELETE ON brain_sites FROM anon, authenticated;
CREATE POLICY "Users can view own brain sites"
    ON brain_sites FOR SELECT USING (auth.uid() = user_id);
