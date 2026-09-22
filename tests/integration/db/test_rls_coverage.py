"""Every table in `public` must have row level security enabled.

Supabase grants the `anon` and `authenticated` roles access to new tables in the
`public` schema by default. A table created without RLS is therefore readable
with the public anon key, and nothing about creating it warns you.

That has already happened twice in this repo. `004_admin_invoice_reconciliation`
created four tables and enabled RLS on none of them, which 014 fixed and
documented as "raw provider invoices, per-user costs and margin data have been
readable with the public anon key". `000_migrations_registry` then did the same
with `schema_migrations`, fixed in 019.

Enumerating the tables rather than listing them: a test that names the tables it
knows about cannot fail for the table someone adds next week, which is the only
failure that matters here.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_every_public_table_has_rls_enabled(migrated_database):
    import psycopg

    with psycopg.connect(migrated_database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relkind = 'r'
               AND NOT c.relrowsecurity
             ORDER BY c.relname
            """
        )
        unprotected = [row[0] for row in cur.fetchall()]

    assert not unprotected, (
        "these tables are readable with the anon key: "
        f"{unprotected}. Add `ALTER TABLE <t> ENABLE ROW LEVEL SECURITY` in a "
        "migration -- with policies if users should reach it, without any if it is "
        "service-role only."
    )


def test_service_role_only_tables_are_revoked_from_the_public_roles(migrated_database):
    """RLS is the mechanism; REVOKE is the belt.

    The tables with no policies at all are the ones only the service role should
    ever touch. RLS already denies every other role, but an accidental policy
    added later would open them again -- the grant being gone is what makes that
    a two-step mistake rather than a one-step one.
    """
    import psycopg

    with psycopg.connect(migrated_database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relkind = 'r'
               AND c.relrowsecurity
               AND NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid)
             ORDER BY c.relname
            """
        )
        policyless = [row[0] for row in cur.fetchall()]

        still_granted = []
        for table in policyless:
            cur.execute(
                """
                SELECT count(*) FROM information_schema.role_table_grants
                 WHERE table_schema = 'public' AND table_name = %s
                   AND grantee IN ('anon', 'authenticated')
                """,
                (table,),
            )
            if cur.fetchone()[0]:
                still_granted.append(table)

    assert not still_granted, (
        f"policy-less tables still granted to anon/authenticated: {still_granted}"
    )
