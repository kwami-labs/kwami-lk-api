-- =============================================================================
-- Close the credit functions to the public roles
-- Postgres grants EXECUTE to PUBLIC on every new function and PostgREST exposes
-- each one at /rest/v1/rpc/<name>. `add_credits` and `deduct_credits` run as their
-- owner and trust the user id they are given, so the public anon key could change
-- anyone's balance. Only the API calls them, with the service role.
--
-- The trigger functions need no grant: Postgres checks EXECUTE when a trigger is
-- created, not each time it fires, so sign-ups keep working.
-- =============================================================================

REVOKE ALL ON FUNCTION add_credits(uuid, bigint, credit_transaction_type, text, jsonb, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION add_credits(uuid, bigint, credit_transaction_type, text, jsonb, text)
    TO service_role;

REVOKE ALL ON FUNCTION deduct_credits(uuid, bigint, text, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION deduct_credits(uuid, bigint, text, jsonb)
    TO service_role;

REVOKE ALL ON FUNCTION handle_new_user_credits() FROM PUBLIC, anon, authenticated;

-- Exists in the hosted project but in no migration, so a fresh database lacks it.
DO $$
BEGIN
    IF to_regprocedure('public.handle_new_user()') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION handle_new_user() FROM PUBLIC, anon, authenticated;
    END IF;
END $$;
