-- =============================================================================
-- Kwami Wallets (Solana)
-- Custodial wallet metadata, token allowlist, funding intents/events, and cache.
-- =============================================================================

CREATE TYPE kwami_wallet_custody_type AS ENUM ('custodial_hsm_mpc', 'external', 'non_custodial_link');
CREATE TYPE kwami_wallet_status AS ENUM ('pending', 'active', 'rotating', 'disabled');
CREATE TYPE wallet_funding_provider AS ENUM ('phantom_transfer', 'card_provider');
CREATE TYPE wallet_funding_status AS ENUM ('pending', 'submitted', 'confirmed', 'failed', 'expired');
CREATE TYPE wallet_event_type AS ENUM ('intent_created', 'provider_update', 'confirmed', 'failed');

CREATE TABLE kwami_wallets (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    chain                   text NOT NULL DEFAULT 'solana',
    network                 text NOT NULL DEFAULT 'mainnet-beta',
    custody_type            kwami_wallet_custody_type NOT NULL DEFAULT 'custodial_hsm_mpc',
    status                  kwami_wallet_status NOT NULL DEFAULT 'pending',
    public_key              text NOT NULL,
    connected_wallet_pubkey text,
    metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kwami_id)
);

CREATE INDEX idx_kwami_wallets_user_kwami ON kwami_wallets(user_id, kwami_id);
CREATE UNIQUE INDEX idx_kwami_wallets_public_key_unique ON kwami_wallets(public_key);

CREATE TABLE kwami_wallet_key_refs (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id                   uuid NOT NULL REFERENCES kwami_wallets(id) ON DELETE CASCADE,
    user_id                     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                    uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    custody_provider            text NOT NULL,
    key_ref                     text NOT NULL,
    key_version                 integer NOT NULL DEFAULT 1,
    encryption_context          jsonb NOT NULL DEFAULT '{}'::jsonb,
    rotation_state              text NOT NULL DEFAULT 'current',
    rotated_at                  timestamptz,
    metadata                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (wallet_id, key_version)
);

CREATE INDEX idx_kwami_wallet_key_refs_user_kwami ON kwami_wallet_key_refs(user_id, kwami_id);

CREATE TABLE wallet_token_allowlist (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chain               text NOT NULL DEFAULT 'solana',
    mint_address        text NOT NULL,
    symbol              text NOT NULL,
    decimals            integer NOT NULL DEFAULT 0,
    is_stablecoin       boolean NOT NULL DEFAULT false,
    is_default          boolean NOT NULL DEFAULT false,
    created_by_user_id  uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chain, mint_address)
);

CREATE INDEX idx_wallet_token_allowlist_symbol ON wallet_token_allowlist(symbol);

CREATE TABLE wallet_funding_intents (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                    uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    wallet_id                   uuid NOT NULL REFERENCES kwami_wallets(id) ON DELETE CASCADE,
    provider                    wallet_funding_provider NOT NULL,
    status                      wallet_funding_status NOT NULL DEFAULT 'pending',
    asset_mint                  text NOT NULL,
    asset_symbol                text NOT NULL,
    expected_amount             numeric(36, 12) NOT NULL,
    expected_amount_usd         numeric(36, 12),
    sender_wallet_pubkey        text,
    destination_wallet_pubkey   text NOT NULL,
    provider_intent_id          text,
    provider_redirect_url       text,
    idempotency_key             text NOT NULL,
    expires_at                  timestamptz,
    metadata                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (idempotency_key)
);

CREATE INDEX idx_wallet_funding_intents_user_kwami ON wallet_funding_intents(user_id, kwami_id, created_at DESC);
CREATE UNIQUE INDEX idx_wallet_funding_intents_provider_intent ON wallet_funding_intents(provider, provider_intent_id)
    WHERE provider_intent_id IS NOT NULL;

CREATE TABLE wallet_funding_events (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                    uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    wallet_id                   uuid NOT NULL REFERENCES kwami_wallets(id) ON DELETE CASCADE,
    intent_id                   uuid NOT NULL REFERENCES wallet_funding_intents(id) ON DELETE CASCADE,
    event_type                  wallet_event_type NOT NULL,
    provider                    wallet_funding_provider NOT NULL,
    provider_event_id           text,
    transaction_signature       text,
    amount_received             numeric(36, 12),
    confirmed_at                timestamptz,
    payload                     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_event_id)
);

CREATE INDEX idx_wallet_funding_events_user_kwami ON wallet_funding_events(user_id, kwami_id, created_at DESC);

CREATE TABLE wallet_balances_cache (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                    uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    wallet_id                   uuid NOT NULL REFERENCES kwami_wallets(id) ON DELETE CASCADE,
    mint_address                text NOT NULL,
    symbol                      text NOT NULL,
    amount                      numeric(36, 12) NOT NULL DEFAULT 0,
    amount_usd                  numeric(36, 12),
    last_onchain_slot           bigint,
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (wallet_id, mint_address)
);

CREATE INDEX idx_wallet_balances_cache_user_kwami ON wallet_balances_cache(user_id, kwami_id, updated_at DESC);

CREATE TABLE wallet_transactions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kwami_id                    uuid NOT NULL REFERENCES user_kwamis(id) ON DELETE CASCADE,
    wallet_id                   uuid NOT NULL REFERENCES kwami_wallets(id) ON DELETE CASCADE,
    mint_address                text NOT NULL,
    symbol                      text NOT NULL,
    direction                   text NOT NULL,
    amount                      numeric(36, 12) NOT NULL,
    amount_usd                  numeric(36, 12),
    transaction_signature       text NOT NULL,
    related_intent_id           uuid REFERENCES wallet_funding_intents(id) ON DELETE SET NULL,
    metadata                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transaction_signature, mint_address, amount, direction)
);

CREATE INDEX idx_wallet_transactions_user_kwami ON wallet_transactions(user_id, kwami_id, created_at DESC);
CREATE INDEX idx_wallet_transactions_wallet_mint ON wallet_transactions(wallet_id, mint_address, created_at DESC);

-- Default token allowlist (mainnet addresses)
INSERT INTO wallet_token_allowlist (chain, mint_address, symbol, decimals, is_stablecoin, is_default)
VALUES
    ('solana', 'So11111111111111111111111111111111111111112', 'SOL', 9, false, true),
    ('solana', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'USDC', 6, true, true),
    ('solana', 'Es9vMFrzaCERf6bxQwzL4wGJPD7W82dManEXZDV4mQ7n', 'USDT', 6, true, true)
ON CONFLICT (chain, mint_address) DO NOTHING;

ALTER TABLE kwami_wallets ENABLE ROW LEVEL SECURITY;
ALTER TABLE kwami_wallet_key_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_token_allowlist ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_funding_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_funding_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_balances_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_transactions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage own kwami wallets"
    ON kwami_wallets FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can manage own wallet key refs"
    ON kwami_wallet_key_refs FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can view token allowlist"
    ON wallet_token_allowlist FOR SELECT
    USING (true);

CREATE POLICY "Users can add custom allowlist tokens"
    ON wallet_token_allowlist FOR INSERT
    WITH CHECK (auth.uid() = created_by_user_id);

CREATE POLICY "Users can update custom allowlist tokens"
    ON wallet_token_allowlist FOR UPDATE
    USING (auth.uid() = created_by_user_id)
    WITH CHECK (auth.uid() = created_by_user_id);

CREATE POLICY "Users can delete custom allowlist tokens"
    ON wallet_token_allowlist FOR DELETE
    USING (auth.uid() = created_by_user_id);

CREATE POLICY "Users can manage own funding intents"
    ON wallet_funding_intents FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can view own funding events"
    ON wallet_funding_events FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can manage own balance cache"
    ON wallet_balances_cache FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can manage own wallet transactions"
    ON wallet_transactions FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE OR REPLACE FUNCTION set_wallet_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER kwami_wallets_updated_at
    BEFORE UPDATE ON kwami_wallets
    FOR EACH ROW
    EXECUTE PROCEDURE set_wallet_updated_at();

CREATE TRIGGER wallet_funding_intents_updated_at
    BEFORE UPDATE ON wallet_funding_intents
    FOR EACH ROW
    EXECUTE PROCEDURE set_wallet_updated_at();
