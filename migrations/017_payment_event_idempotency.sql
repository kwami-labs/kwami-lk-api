-- =============================================================================
-- Webhook and usage-report idempotency.
--
-- Stripe retries any non-2xx and can legitimately redeliver an event it already
-- sent. The webhook handler verified the signature and then credited
-- unconditionally: no event id was stored, there was no unique constraint on the
-- checkout session, and no pre-check. Every retry granted the credits again.
--
-- The agent's usage report had the mirror problem: no idempotency key at all, so
-- a replayed report charged the user twice for one session.
--
-- Both are fixed with a claim-before-process table plus a uniqueness guarantee on
-- the ledger itself, because "have I already done this?" and "do it" must be the
-- same transaction to survive two concurrent deliveries.
-- =============================================================================

-- 1. Provider webhook receipts -------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_events (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider     text NOT NULL CHECK (provider IN ('stripe', 'wallet', 'twilio', 'sendgrid')),
    event_id     text NOT NULL,
    event_type   text NOT NULL,
    status       text NOT NULL DEFAULT 'received'
                 CHECK (status IN ('received', 'processed', 'ignored', 'failed')),
    attempts     int  NOT NULL DEFAULT 0,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    result       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text,
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    -- The dedup guarantee. Claiming an event is an INSERT that either wins or
    -- conflicts; there is no read-then-write window for a retry to slip through.
    UNIQUE (provider, event_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_events_unprocessed
    ON payment_events(provider, received_at)
    WHERE status IN ('received', 'failed');

COMMENT ON TABLE payment_events IS
    'One row per provider webhook delivery. UNIQUE(provider, event_id) is what makes redelivery safe.';

-- 2. Usage report receipts -----------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_reports (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_key      text NOT NULL UNIQUE,
    user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    session_id      text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'settled', 'partially_settled', 'failed')),
    items_count     int    NOT NULL DEFAULT 0,
    requested_micro bigint NOT NULL DEFAULT 0 CHECK (requested_micro >= 0),
    charged_micro   bigint NOT NULL DEFAULT 0 CHECK (charged_micro >= 0),
    -- What the user owed but could not pay. Previously an over-balance report
    -- charged zero, so the session was simply given away.
    unpaid_micro    bigint NOT NULL DEFAULT 0 CHECK (unpaid_micro >= 0),
    -- The response is cached so a replay returns the original answer rather than
    -- recomputing against a balance that has since moved.
    result          jsonb  NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    settled_at      timestamptz
);

CREATE INDEX IF NOT EXISTS idx_usage_reports_session
    ON usage_reports(user_id, session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_reports_stuck
    ON usage_reports(status, created_at)
    WHERE status IN ('pending', 'failed');

-- 3. Ledger-level uniqueness ---------------------------------------------------
-- The backstop: even if two different events describe the same payment, only one
-- can produce a ledger row.
ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS idempotency_key text;

-- Backfill from existing purchases before the unique index is built. Rows ranked
-- beyond the first for a session are pre-existing DOUBLE CREDITS caused by the
-- missing dedup; they are left NULL (so the index can build) and reported below
-- rather than silently reversed -- clawing back credits is a business decision.
WITH ranked AS (
    SELECT id,
           metadata ->> 'stripe_session_id' AS session_id,
           row_number() OVER (
               PARTITION BY metadata ->> 'stripe_session_id'
               ORDER BY created_at
           ) AS rn
    FROM credit_transactions
    WHERE type = 'purchase'
      AND metadata ? 'stripe_session_id'
      AND idempotency_key IS NULL
)
UPDATE credit_transactions t
   SET idempotency_key = 'stripe:session:' || r.session_id
  FROM ranked r
 WHERE t.id = r.id AND r.rn = 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_idempotency
    ON credit_transactions(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMENT ON COLUMN credit_transactions.idempotency_key IS
    'Namespaced key: stripe:session:<id>, stripe:refund:<ch>:<re>, wallet:intent:<uuid>, usage:report:<key>, bonus:welcome:<user>.';

-- Anything this view returns was credited more than once for a single payment.
CREATE OR REPLACE VIEW credit_transactions_suspected_duplicates AS
SELECT metadata ->> 'stripe_session_id' AS stripe_session_id,
       user_id,
       count(*)   AS ledger_rows,
       sum(amount) AS total_micro_credits,
       min(created_at) AS first_credited_at,
       max(created_at) AS last_credited_at
FROM credit_transactions
WHERE type = 'purchase'
  AND metadata ? 'stripe_session_id'
GROUP BY 1, 2
HAVING count(*) > 1;

COMMENT ON VIEW credit_transactions_suspected_duplicates IS
    'Checkout sessions credited more than once, from before webhook dedup existed. Should be empty.';

-- 4. Settlement outcomes -------------------------------------------------------
ALTER TABLE credit_usage_logs
    ADD COLUMN IF NOT EXISTS report_id uuid REFERENCES usage_reports(id) ON DELETE SET NULL;

ALTER TABLE credit_usage_logs DROP CONSTRAINT IF EXISTS credit_usage_logs_settlement_status_check;
ALTER TABLE credit_usage_logs ADD CONSTRAINT credit_usage_logs_settlement_status_check
    CHECK (settlement_status IN (
        'pending', 'charged', 'partially_charged',
        'insufficient_credits', 'written_off', 'skipped', 'failed'
    ));

-- 5. Balances must never go negative ------------------------------------------
-- NOT VALID so the deploy cannot fail on legacy rows; VALIDATE after a sweep.
ALTER TABLE user_credits DROP CONSTRAINT IF EXISTS user_credits_balance_nonnegative;
ALTER TABLE user_credits ADD CONSTRAINT user_credits_balance_nonnegative
    CHECK (balance >= 0) NOT VALID;

-- 6. Lock the new tables down --------------------------------------------------
ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_reports  ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON payment_events FROM anon, authenticated;

CREATE POLICY "Users can view own usage reports"
    ON usage_reports FOR SELECT
    USING (auth.uid() = user_id);

-- 7. add_credits gains an idempotency key -------------------------------------
-- The uniqueness check and the balance mutation must be the SAME transaction, or
-- two concurrent Stripe retries both see "not credited yet" and both credit. The
-- insert is therefore done first, with ON CONFLICT DO NOTHING: whoever loses the
-- race writes no ledger row, touches no balance, and returns the current one.
--
-- Dropped and recreated rather than CREATE OR REPLACEd: adding a parameter makes
-- a new overload rather than replacing the function, and two candidates would
-- make PostgREST's named-argument dispatch ambiguous.
DROP FUNCTION IF EXISTS add_credits(uuid, bigint, credit_transaction_type, text, jsonb);

CREATE FUNCTION add_credits(
    p_user_id         uuid,
    p_amount          bigint,
    p_type            credit_transaction_type DEFAULT 'purchase',
    p_description     text DEFAULT NULL,
    p_metadata        jsonb DEFAULT '{}',
    p_idempotency_key text DEFAULT NULL
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    new_balance      bigint;
    transaction_id   uuid;
BEGIN
    IF p_amount IS NULL OR p_amount <= 0 THEN
        RAISE EXCEPTION 'add_credits requires a positive amount, got %', p_amount;
    END IF;

    IF p_idempotency_key IS NOT NULL THEN
        -- Claim the key first. Zero rows means another transaction already
        -- credited this payment, so return the balance unchanged.
        INSERT INTO credit_transactions
            (user_id, type, amount, balance_after, description, metadata, idempotency_key)
        VALUES
            (p_user_id, p_type, p_amount, 0, p_description, p_metadata, p_idempotency_key)
        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
        RETURNING id INTO transaction_id;

        IF transaction_id IS NULL THEN
            SELECT balance INTO new_balance FROM user_credits WHERE user_id = p_user_id;
            RETURN COALESCE(new_balance, 0);
        END IF;
    END IF;

    INSERT INTO user_credits (user_id, balance, lifetime_purchased, lifetime_used)
    VALUES (p_user_id, p_amount, p_amount, 0)
    ON CONFLICT (user_id) DO UPDATE
    SET balance            = user_credits.balance + p_amount,
        lifetime_purchased = user_credits.lifetime_purchased + p_amount,
        updated_at         = now()
    RETURNING balance INTO new_balance;

    IF transaction_id IS NULL THEN
        INSERT INTO credit_transactions
            (user_id, type, amount, balance_after, description, metadata)
        VALUES (p_user_id, p_type, p_amount, new_balance, p_description, p_metadata);
    ELSE
        -- balance_after was not knowable before the balance moved.
        UPDATE credit_transactions SET balance_after = new_balance WHERE id = transaction_id;
    END IF;

    RETURN new_balance;
END;
$$;

COMMENT ON FUNCTION add_credits IS
    'Atomically credit a user. Passing p_idempotency_key makes the call safe to retry: a repeat is a no-op returning the current balance.';
