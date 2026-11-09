-- =============================================================================
-- Where Jarvis may go on the web, and what it may do there.
--
-- One row per site the user set, plus the pattern '*' for every other site. A
-- pattern is a host ("github.com") and covers its subdomains. Jarvis can ask for
-- a site it has no rule for: that row waits as 'pending' until the user answers
-- in the app, and until then it changes nothing.
--
-- Writes go through the API, which checks every address, or the agent with the
-- service role. A user reads their own rows and cannot write them directly.
-- =============================================================================

CREATE TABLE jarvis_site_rules (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    pattern    text NOT NULL CHECK (char_length(pattern) BETWEEN 1 AND 253),
    access     text NOT NULL
               CHECK (access IN ('blocked', 'read', 'browse', 'write', 'account')),
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending')),
    reason     text CHECK (reason IS NULL OR char_length(reason) <= 300),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, pattern)
);

COMMENT ON TABLE jarvis_site_rules IS
    'Sites Jarvis may visit and what it may do on each; ''*'' is every other site.';

ALTER TABLE jarvis_site_rules ENABLE ROW LEVEL SECURITY;
REVOKE INSERT, UPDATE, DELETE ON jarvis_site_rules FROM anon, authenticated;

CREATE POLICY "Users can view own site rules"
    ON jarvis_site_rules FOR SELECT
    USING (auth.uid() = user_id);

CREATE OR REPLACE FUNCTION set_jarvis_site_rules_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER jarvis_site_rules_updated_at
    BEFORE UPDATE ON jarvis_site_rules
    FOR EACH ROW
    EXECUTE PROCEDURE set_jarvis_site_rules_updated_at();
