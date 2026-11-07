-- =============================================================================
-- The brain in the cloud: work that goes on with the PC switched off
--
-- Until now only the process on the user's PC took jobs from `brain_jobs`, so a
-- job asked from the phone, or fired by the clock, waited for the PC. Here a
-- second kind of worker joins: one in the cloud, shared by every user who allows
-- it, that takes the work the PC is not there for. It has no access to the
-- user's files, so what it makes is kept in Storage and listed in `brain_sync`,
-- and the PC brings it home the next time it is on, without stopping either side.
--
-- And the agents a user defines from the app (`brain_agents`): the specialists
-- the brain can hand a task to, run now or on the clock, on the PC or in the cloud.
--
-- As everywhere in the brain, only the service role writes: a row here makes a
-- program run, and the app asks for changes through the API.
-- =============================================================================

-- Where a job may run.
--   auto:  the PC when it is there; the cloud when it is not and the user allowed it.
--   pc:    waits for the PC, however long that takes.
--   cloud: the cloud, even with the PC on.
ALTER TABLE brain_jobs ADD COLUMN target text NOT NULL DEFAULT 'auto'
    CHECK (target IN ('auto', 'pc', 'cloud'));

-- Jobs asked from the app, on the phone or the PC, with no call behind them.
ALTER TABLE brain_jobs DROP CONSTRAINT IF EXISTS brain_jobs_source_check;
ALTER TABLE brain_jobs ADD CONSTRAINT brain_jobs_source_check
    CHECK (source IN ('call', 'schedule', 'app'));

-- Those are told how they ended, as scheduled work is (`notify_finished_work`).
DROP INDEX IF EXISTS idx_brain_jobs_unnotified;
CREATE INDEX idx_brain_jobs_unnotified ON brain_jobs(created_at)
    WHERE source IN ('schedule', 'app') AND notified_at IS NULL AND status IN ('done', 'failed');

-- Whether the user lets the cloud work for them. Off until they turn it on: it
-- spends on an API key, where the PC spends the plan of its own Claude login.
CREATE TABLE brain_cloud_settings (
    user_id    uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    enabled    boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per cloud worker, the proof it is alive. Not per user: one serves many.
CREATE TABLE brain_cloud_runners (
    runner_id    text PRIMARY KEY,
    hostname     text NOT NULL DEFAULT '',
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

-- The specialists a user defines. The brain hands them a task by name, and the
-- clock can run one on a schedule. Their tools can never be more than the
-- executor's own: the agent filters them again before a run.
CREATE TABLE brain_agents (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name        text NOT NULL CHECK (name ~ '^[a-z][a-z0-9-]{1,39}$'),
    description text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 500),
    prompt      text NOT NULL CHECK (char_length(prompt) BETWEEN 1 AND 8000),
    model       text NOT NULL DEFAULT 'claude-sonnet-5-5',
    tools       text[] NOT NULL DEFAULT '{Read,Glob,Grep}',
    effort      text NOT NULL DEFAULT 'medium' CHECK (effort IN ('low', 'medium', 'high')),
    target      text NOT NULL DEFAULT 'auto' CHECK (target IN ('auto', 'pc', 'cloud')),
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE INDEX idx_brain_agents_user ON brain_agents(user_id, name);

-- The agent a job runs as.
ALTER TABLE brain_jobs ADD COLUMN agent_id uuid REFERENCES brain_agents(id) ON DELETE SET NULL;

-- Scheduled work can name the agent that does it, and where.
ALTER TABLE kwami_assistant_tasks
    ADD COLUMN agent_id uuid REFERENCES brain_agents(id) ON DELETE SET NULL;
ALTER TABLE kwami_assistant_tasks ADD COLUMN target text NOT NULL DEFAULT 'auto'
    CHECK (target IN ('auto', 'pc', 'cloud'));

-- What the cloud made that belongs on the PC: files to copy into the Jarvis folder,
-- or a patch to Jarvis's own code that the PC turns into a pull request (the cloud
-- holds no GitHub credentials). The PC takes one at a time, `pending` -> `applying`,
-- and never overwrites a file of the user's: a clash is kept beside it.
CREATE TABLE brain_sync (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id       uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    job_id         uuid REFERENCES brain_jobs(id) ON DELETE SET NULL,
    kind           text NOT NULL CHECK (kind IN ('files', 'patch')),
    repo           text NOT NULL DEFAULT '' CHECK (repo IN ('', 'agent', 'api', 'app', 'sdk')),
    -- The commit a patch was made on, so the PC can apply it three ways.
    base_ref       text NOT NULL DEFAULT '',
    -- In the `jarvis-files` bucket.
    storage_prefix text NOT NULL,
    files          integer NOT NULL DEFAULT 0 CHECK (files >= 0),
    title          text NOT NULL DEFAULT '',
    status         text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'applying', 'applied', 'conflict', 'failed', 'discarded')),
    result         text NOT NULL DEFAULT '',
    runner_id      text NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now(),
    applied_at     timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_brain_sync_pending ON brain_sync(user_id, created_at) WHERE status = 'pending';
CREATE INDEX idx_brain_sync_user ON brain_sync(user_id, created_at DESC);

CREATE TRIGGER brain_sync_updated_at
    BEFORE UPDATE ON brain_sync
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();
CREATE TRIGGER brain_agents_updated_at
    BEFORE UPDATE ON brain_agents
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();
CREATE TRIGGER brain_cloud_settings_updated_at
    BEFORE UPDATE ON brain_cloud_settings
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();

ALTER TABLE brain_cloud_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE brain_cloud_runners ENABLE ROW LEVEL SECURITY;
ALTER TABLE brain_agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE brain_sync ENABLE ROW LEVEL SECURITY;

REVOKE INSERT, UPDATE, DELETE ON brain_cloud_settings FROM anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON brain_agents FROM anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON brain_sync FROM anon, authenticated;
-- Says nothing about any one user, and nobody but the workers and the API needs it.
REVOKE ALL ON brain_cloud_runners FROM anon, authenticated;

CREATE POLICY "Users can view own brain cloud settings"
    ON brain_cloud_settings FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can view own brain agents"
    ON brain_agents FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can view own brain sync"
    ON brain_sync FOR SELECT USING (auth.uid() = user_id);

-- The PC no longer takes what was asked for the cloud. Same signature, so it is
-- replaced in place and the worker on the PC needs no change.
CREATE OR REPLACE FUNCTION brain_claim_job(p_user_id uuid, p_runner_id text, p_thinking boolean DEFAULT false)
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
           AND target <> 'cloud'
           AND (mode <> 'work') = p_thinking
         ORDER BY created_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED
     )
    RETURNING *;
$$;

REVOKE ALL ON FUNCTION brain_claim_job(uuid, text, boolean) FROM PUBLIC, anon, authenticated;

-- The oldest job the cloud may take, of any user, or nothing:
--   * work only: thinking belongs to a call, and orders (merge, login, site) to the PC;
--   * of a user who allowed the cloud;
--   * asked for the cloud, or `auto` with no PC of its user seen in the last 90 s
--     (the PC's RUNNER_FRESH_SECONDS) and queued at least `p_patience_seconds` ago,
--     so a PC that is just switching on gets the first go;
--   * never more than `p_per_user` of one user at once, so one long queue cannot
--     hold the cloud for everyone else, nor run up a bill in parallel.
-- SKIP LOCKED, so two cloud workers never take the same job.
CREATE FUNCTION brain_claim_cloud_job(
    p_runner_id text,
    p_patience_seconds integer DEFAULT 60,
    p_per_user integer DEFAULT 2
)
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
        SELECT j.id
          FROM brain_jobs j
          JOIN brain_cloud_settings s ON s.user_id = j.user_id AND s.enabled
         WHERE j.status = 'queued'
           AND j.mode = 'work'
           AND (
                j.target = 'cloud'
             OR (
                    j.target = 'auto'
                AND j.created_at <= now() - make_interval(secs => p_patience_seconds)
                AND NOT EXISTS (
                        SELECT 1 FROM brain_runners r
                         WHERE r.user_id = j.user_id
                           AND r.last_seen_at > now() - interval '90 seconds'
                    )
                )
           )
           AND (
                SELECT count(*) FROM brain_jobs busy
                 WHERE busy.user_id = j.user_id
                   AND busy.status = 'running'
                   AND busy.runner_id LIKE 'cloud-%'
               ) < p_per_user
         ORDER BY j.created_at
         LIMIT 1
         FOR UPDATE OF j SKIP LOCKED
     )
    RETURNING *;
$$;

REVOKE ALL ON FUNCTION brain_claim_cloud_job(text, integer, integer) FROM PUBLIC, anon, authenticated;

-- The next thing the cloud left for this user's PC, taken so no second PC applies it too.
CREATE FUNCTION brain_claim_sync(p_user_id uuid, p_runner_id text)
RETURNS SETOF brain_sync
LANGUAGE sql
SET search_path = public
AS $$
    UPDATE brain_sync
       SET status = 'applying',
           runner_id = p_runner_id
     WHERE id = (
        SELECT id FROM brain_sync
         WHERE user_id = p_user_id AND status = 'pending'
         ORDER BY created_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED
     )
    RETURNING *;
$$;

REVOKE ALL ON FUNCTION brain_claim_sync(uuid, text) FROM PUBLIC, anon, authenticated;
