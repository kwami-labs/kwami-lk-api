-- Orders for the PC that need no model: merging Jarvis's own pull request on the
-- user's word, and opening Claude's sign-in. brain_claim_job hands them to the
-- worker's quick lane with the thinking jobs, so it does not change.

ALTER TABLE brain_jobs DROP CONSTRAINT IF EXISTS brain_jobs_mode_check;
ALTER TABLE brain_jobs ADD CONSTRAINT brain_jobs_mode_check
    CHECK (mode IN ('work', 'think', 'research', 'merge', 'login'));
