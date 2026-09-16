-- =============================================================================
-- Migration registry
--
-- Until now migrations were pasted into the Supabase SQL editor by hand, with
-- nothing recording which files had run -- which is how two files both came to be
-- numbered 002 without anyone noticing. This table is the record.
--
-- Existing databases are marked up to date once with:
--     uv run python scripts/migrate.py --baseline 010
-- which stamps 001-010 as applied without re-running them. Fresh databases run
-- every file in order and land in the same state.
-- =============================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    name       text NOT NULL,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE schema_migrations IS
    'One row per applied migration file. Managed by scripts/migrate.py.';
COMMENT ON COLUMN schema_migrations.checksum IS
    'sha256 of the file contents when applied. A mismatch means an applied migration was edited.';
