-- =============================================================================
-- The mind: thinking on the Claude account of the user's PC, and the model keys
-- a user connects from the app.
-- =============================================================================

-- Jobs that only answer run beside the work, with no files and no commands.
ALTER TABLE brain_jobs ADD COLUMN mode text NOT NULL DEFAULT 'work'
    CHECK (mode IN ('work', 'think', 'research'));

CREATE INDEX idx_brain_jobs_thinking ON brain_jobs(user_id, created_at)
    WHERE status = 'queued' AND mode <> 'work';

-- Whether the worker's PC is signed in to a Claude account. A yes or a no: the
-- credentials stay in Claude Code, on that PC.
ALTER TABLE brain_runners ADD COLUMN claude_login boolean NOT NULL DEFAULT false;

DROP FUNCTION brain_claim_job(uuid, text);

-- The oldest queued job of this user of the kind asked for, or nothing. SKIP LOCKED
-- so two processes never take the same one.
CREATE FUNCTION brain_claim_job(p_user_id uuid, p_runner_id text, p_thinking boolean DEFAULT false)
RETURNS SETOF brain_jobs
LANGUAGE sql
SET search_path = public
AS $$
    UPDATE brain_jobs
       SET status = 'running',
           runner_id = p_runner_id,
           started_at = now(),
           heartbeat_at = now()
     WHERE id = (
        SELECT id FROM brain_jobs
         WHERE user_id = p_user_id
           AND status = 'queued'
           AND (mode <> 'work') = p_thinking
         ORDER BY created_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED
     )
    RETURNING *;
$$;

REVOKE ALL ON FUNCTION brain_claim_job(uuid, text, boolean) FROM PUBLIC, anon, authenticated;

-- One key per user and provider, encrypted before it gets here. Nobody reads it
-- through the public roles, not even its owner: the app sees only `key_hint`,
-- through the API.
CREATE TABLE kwami_provider_keys (
    user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    provider      text NOT NULL CHECK (
        provider IN ('anthropic', 'google', 'openai', 'xai', 'openrouter', 'groq', 'deepseek', 'mistral')
    ),
    encrypted_key text NOT NULL,
    -- The last characters, so the user can tell which key this is.
    key_hint      text NOT NULL DEFAULT '',
    connected_at  timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider)
);

ALTER TABLE kwami_provider_keys ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON kwami_provider_keys FROM anon, authenticated;

CREATE TRIGGER kwami_provider_keys_updated_at
    BEFORE UPDATE ON kwami_provider_keys
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();
