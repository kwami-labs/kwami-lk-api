-- =============================================================================
-- Per-user app settings (UI locale, etc.)
-- Run after 002_user_kwamis.sql (requires auth.users).
-- =============================================================================

CREATE TABLE user_app_settings (
    user_id    uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    locale     text NOT NULL DEFAULT 'en' CHECK (locale IN ('en', 'es')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE user_app_settings IS 'Cross-device preferences for the Kwami app (locale, future keys).';

ALTER TABLE user_app_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own app settings"
    ON user_app_settings FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own app settings"
    ON user_app_settings FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own app settings"
    ON user_app_settings FOR UPDATE
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE OR REPLACE FUNCTION set_user_app_settings_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER user_app_settings_updated_at
    BEFORE UPDATE ON user_app_settings
    FOR EACH ROW
    EXECUTE PROCEDURE set_user_app_settings_updated_at();
