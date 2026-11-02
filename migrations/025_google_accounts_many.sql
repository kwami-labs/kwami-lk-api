-- =============================================================================
-- More than one Google account per user
-- The user works from several accounts that need not share an address, so the key
-- becomes the pair. `needs_reconnect_at` is set when Google stops honouring a
-- refresh token, which is the one way a link dies on its own, so the app can say so.
-- =============================================================================

ALTER TABLE kwami_google_accounts DROP CONSTRAINT kwami_google_accounts_pkey;
ALTER TABLE kwami_google_accounts ADD PRIMARY KEY (user_id, email);
ALTER TABLE kwami_google_accounts ADD COLUMN needs_reconnect_at timestamptz;
