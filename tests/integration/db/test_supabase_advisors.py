"""What the Supabase advisors flag stays fixed (migration 031).

Each check reads the catalog rather than a list of names, so the table, view or
function someone adds next week is held to it too.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def test_no_policy_calls_auth_uid_once_per_row(db):
    """`auth.uid()` bare is evaluated for every row; `(select auth.uid())` once."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT tablename, policyname
              FROM pg_policies
             WHERE schemaname = 'public'
               AND (coalesce(qual, '') ~* '(?<!select )auth\\.uid\\(\\)'
                 OR coalesce(with_check, '') ~* '(?<!select )auth\\.uid\\(\\)')
             ORDER BY 1, 2
            """
        )
        bare = cur.fetchall()
    assert not bare, f"policies that call auth.uid() per row: {bare}"


def test_no_view_runs_with_its_owners_rights_for_the_public_roles(db):
    """A definer view is read with its owner's rights, so RLS on its tables does not apply."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relkind = 'v'
               AND NOT coalesce('security_invoker=true' = ANY (c.reloptions), false)
               AND (has_table_privilege('anon', c.oid, 'SELECT')
                 OR has_table_privilege('authenticated', c.oid, 'SELECT'))
             ORDER BY 1
            """
        )
        open_views = [row[0] for row in cur.fetchall()]
    assert not open_views, f"definer views the public roles can read: {open_views}"


def test_every_trigger_function_of_ours_has_a_fixed_search_path(db):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT p.proname
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public'
               AND p.prorettype = 'trigger'::regtype
               AND NOT coalesce(
                   EXISTS (SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%'),
                   false)
             ORDER BY 1
            """
        )
        mutable = [row[0] for row in cur.fetchall()]
    assert not mutable, f"trigger functions with a mutable search_path: {mutable}"


def test_every_foreign_key_has_an_index_that_starts_with_it(db):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.conrelid::regclass::text, c.conname
              FROM pg_constraint c
             WHERE c.contype = 'f'
               AND c.connamespace = 'public'::regnamespace
               AND NOT EXISTS (
                   SELECT 1 FROM pg_index i
                    WHERE i.indrelid = c.conrelid
                      AND (i.indkey::int2[])[0:array_length(c.conkey, 1) - 1] = c.conkey
               )
             ORDER BY 1, 2
            """
        )
        unindexed = cur.fetchall()
    assert not unindexed, f"foreign keys with no covering index: {unindexed}"


def test_the_policies_still_keep_users_apart(db):
    """Rewriting them must not change who sees what: a user sees their own rows only."""
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    with db.cursor() as cur:
        for user_id in (mine, theirs):
            cur.execute(
                "INSERT INTO auth.users (id, email) VALUES (%s, %s)",
                (user_id, f"{user_id}@example.com"),
            )
            cur.execute("INSERT INTO user_kwamis (user_id, name) VALUES (%s, 'k')", (user_id,))
        cur.execute("SET ROLE authenticated")
        try:
            cur.execute("SELECT set_config('request.jwt.claim.sub', %s, false)", (mine,))
            cur.execute("SELECT user_id::text FROM user_kwamis")
            seen = {row[0] for row in cur.fetchall()}
            cur.execute("SELECT user_id::text FROM user_credits")
            credits = {row[0] for row in cur.fetchall()}
        finally:
            cur.execute("RESET ROLE")
            cur.execute("SELECT set_config('request.jwt.claim.sub', '', false)")
    assert seen == {mine}
    assert credits <= {mine}


def test_the_duplicates_view_is_for_the_service_role_only(db):
    import psycopg

    with db.cursor() as cur:
        cur.execute("SET ROLE anon")
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM credit_transactions_suspected_duplicates")
        finally:
            cur.execute("RESET ROLE")
