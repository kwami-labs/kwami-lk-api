-- =============================================================================
-- The brain's work thread
-- The queue between whoever asks for work (a call, the clock) and the process on
-- the user's PC that does it, and the proof that process is alive.
--
-- A row here makes a program run on the user's PC, so only the service role may
-- write: the app reads its own rows and asks for changes through the API.
-- =============================================================================

CREATE TABLE brain_jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id         uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    -- Follow-ups share a thread, and the worker resumes its Claude session.
    thread_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    source           text NOT NULL DEFAULT 'call' CHECK (source IN ('call', 'schedule')),
    schedule_task_id uuid REFERENCES kwami_assistant_tasks(id) ON DELETE SET NULL,
    title            text NOT NULL,
    task             text NOT NULL,
    difficulty       text NOT NULL DEFAULT 'normal' CHECK (difficulty IN ('simple', 'normal', 'hard')),
    model            text NOT NULL DEFAULT '',
    -- Set when the job changes Jarvis's own code. It ends in a pull request.
    repo             text NOT NULL DEFAULT '' CHECK (repo IN ('', 'agent', 'api', 'app', 'sdk')),
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    cancel_requested boolean NOT NULL DEFAULT false,
    progress         text NOT NULL DEFAULT '',
    result           text NOT NULL DEFAULT '',
    cost_usd         numeric(10, 6) NOT NULL DEFAULT 0,
    sdk_session_id   text NOT NULL DEFAULT '',
    runner_id        text NOT NULL DEFAULT '',
    -- Renewed by the worker while it runs: a stale one means the PC went away.
    heartbeat_at     timestamptz,
    -- Renewed by a live call watching the job: while fresh, the call says the result.
    watcher_seen_at  timestamptz,
    -- Someone took charge of telling the user: a call, or the greeting's list.
    delivered_at     timestamptz,
    -- The phone notice of a scheduled job went out.
    notified_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_brain_jobs_queue ON brain_jobs(user_id, created_at) WHERE status = 'queued';
CREATE INDEX idx_brain_jobs_running ON brain_jobs(user_id, heartbeat_at) WHERE status = 'running';
CREATE INDEX idx_brain_jobs_thread ON brain_jobs(thread_id, created_at DESC);
CREATE INDEX idx_brain_jobs_kwami ON brain_jobs(user_id, kwami_id, created_at DESC);
CREATE INDEX idx_brain_jobs_unnotified ON brain_jobs(created_at)
    WHERE source = 'schedule' AND notified_at IS NULL AND status IN ('done', 'failed');

-- One row per process on a PC. A call sends work there only if `last_seen_at` is fresh.
CREATE TABLE brain_runners (
    user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    runner_id    text NOT NULL,
    hostname     text NOT NULL DEFAULT '',
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, runner_id)
);

ALTER TABLE brain_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE brain_runners ENABLE ROW LEVEL SECURITY;

REVOKE INSERT, UPDATE, DELETE ON brain_jobs FROM anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON brain_runners FROM anon, authenticated;

CREATE POLICY "Users can view own brain jobs"
    ON brain_jobs FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can view own brain runners"
    ON brain_runners FOR SELECT USING (auth.uid() = user_id);

CREATE TRIGGER brain_jobs_updated_at
    BEFORE UPDATE ON brain_jobs
    FOR EACH ROW EXECUTE PROCEDURE set_kwami_communications_updated_at();

-- The oldest queued job of this user, or nothing. SKIP LOCKED so two processes
-- never take the same one.
CREATE FUNCTION brain_claim_job(p_user_id uuid, p_runner_id text)
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
         WHERE user_id = p_user_id AND status = 'queued'
         ORDER BY created_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED
     )
    RETURNING *;
$$;

REVOKE ALL ON FUNCTION brain_claim_job(uuid, text) FROM PUBLIC, anon, authenticated;

-- Work the user defines to run on a schedule: `notes` is the instruction.
ALTER TABLE kwami_assistant_tasks DROP CONSTRAINT kwami_assistant_tasks_kind_check;
ALTER TABLE kwami_assistant_tasks ADD CONSTRAINT kwami_assistant_tasks_kind_check
    CHECK (kind IN ('reminder', 'followup', 'report', 'work'));
