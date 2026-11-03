-- =============================================================================
-- The panel layout follows the user across devices: which panels show, their
-- order and their size. The existing policies already let a user write their row.
-- =============================================================================

ALTER TABLE user_app_settings ADD COLUMN ui jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN user_app_settings.ui IS 'Panel layout written by the app; see stores/ui.ts.';
