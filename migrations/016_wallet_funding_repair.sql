-- =============================================================================
-- Wallet funding: make crediting idempotent, and expose what needs repairing.
--
-- What was broken
-- ---------------
-- settle_funding_intent set the intent to 'confirmed' BEFORE calling add_credits,
-- and passed transaction_type='wallet_funding' -- a value that is not in
-- credit_transaction_type ('purchase','usage','bonus','refund'), so the RPC
-- always raised. Every wallet funding therefore took the deposit and granted no
-- credits. Worse, the retry path short-circuited on status='confirmed', so the
-- call could never succeed on a second attempt: money in, credits never issued,
-- no way back without manual SQL.
--
-- The code now credits first and confirms last, keys the retry check on the
-- ledger rather than the intent status, and records the funding as a 'purchase'
-- carrying metadata.source='wallet'. A new enum value was deliberately avoided:
-- enum values can never be removed, and ALTER TYPE ... ADD VALUE cannot run in a
-- transaction.
-- =============================================================================

-- credit_transactions.metadata was unindexed jsonb, so "have I already credited
-- this intent?" was a sequential scan of the whole ledger.
CREATE INDEX IF NOT EXISTS idx_credit_transactions_metadata
    ON credit_transactions USING gin (metadata jsonb_path_ops);

-- Intents that were confirmed but never credited: the exact set damaged by the
-- bug above. Repair is a deliberate, reviewed action -- run it, export the rows
-- for finance sign-off, then credit them.
CREATE OR REPLACE VIEW wallet_funding_intents_unpaid AS
SELECT
    i.id            AS intent_id,
    i.user_id,
    i.kwami_id,
    i.provider,
    i.asset_symbol,
    i.asset_mint,
    i.expected_amount,
    i.created_at,
    i.updated_at
FROM wallet_funding_intents i
WHERE i.status = 'confirmed'
  AND NOT EXISTS (
      SELECT 1
      FROM credit_transactions t
      WHERE t.user_id = i.user_id
        AND t.metadata ->> 'intent_id' = i.id::text
  );

COMMENT ON VIEW wallet_funding_intents_unpaid IS
    'Funding intents marked confirmed with no matching credit_transactions row. Should be empty; anything here is money taken without credits granted.';
