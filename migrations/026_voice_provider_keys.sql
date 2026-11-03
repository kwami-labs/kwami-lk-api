-- Keys for voice-only providers, so a user can run a whole voice pipeline on their own.

ALTER TABLE kwami_provider_keys DROP CONSTRAINT IF EXISTS kwami_provider_keys_provider_check;
ALTER TABLE kwami_provider_keys ADD CONSTRAINT kwami_provider_keys_provider_check CHECK (
    provider IN (
        'anthropic', 'google', 'openai', 'xai', 'openrouter', 'groq', 'deepseek', 'mistral',
        'deepgram', 'elevenlabs', 'cartesia', 'assemblyai'
    )
);
