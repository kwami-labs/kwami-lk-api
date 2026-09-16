-- Supabase-provided objects that migrations depend on but do not create.
--
-- Seven tables reference auth.users, every RLS policy calls auth.uid(), and the
-- anon / authenticated / service_role roles are granted or revoked throughout.
-- None of that exists in a stock Postgres, so the integration harness stands it
-- up before applying migrations/*.sql unchanged.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text
);

-- PostgREST exposes the JWT subject through this GUC; the policies read it here.
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;

DO $$ BEGIN CREATE ROLE anon NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE authenticated NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE service_role NOLOGIN BYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO anon, authenticated;
