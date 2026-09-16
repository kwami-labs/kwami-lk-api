"""The atomicity guarantees, proven against Postgres rather than modelled.

These are the assertions the in-process fake cannot make: they depend on a
single-statement conditional UPDATE, a partial unique index, an enum domain and
real transaction isolation.
"""

from __future__ import annotations

import concurrent.futures
import uuid

import pytest

pytestmark = pytest.mark.integration

MICRO = 1_000  # micro-credits per credit


def balance_of(db, user_id: str) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT balance FROM user_credits WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 0


def ledger_rows(db, user_id: str) -> list[tuple]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT type, amount, balance_after, idempotency_key "
            "FROM credit_transactions WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        )
        return cur.fetchall()


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------


def test_contacts_columns_exist(db):
    """009 had a stray `;` that stopped it creating instagram and tiktok.

    Code writes and filters on both, so contact create, update and search were
    all returning 500 in production.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'kwami_contacts'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert {"email", "notes", "instagram", "tiktok"} <= columns


def test_reconciliation_tables_have_rls_enabled(db):
    """004 created four tables holding provider invoices and per-user margin
    data and never enabled RLS, leaving them readable with the anon key."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT tablename, rowsecurity FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename LIKE 'provider_%'"
        )
        flags = dict(cur.fetchall())
    assert flags, "expected the provider_* tables to exist"
    assert all(flags.values()), f"RLS is off for: {[t for t, on in flags.items() if not on]}"


def test_every_user_scoped_table_has_rls(db):
    """A standing invariant, so the next migration cannot quietly forget it."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT t.tablename, t.rowsecurity
            FROM pg_tables t
            JOIN information_schema.columns c
              ON c.table_name = t.tablename AND c.table_schema = t.schemaname
            WHERE t.schemaname = 'public' AND c.column_name = 'user_id'
            GROUP BY t.tablename, t.rowsecurity
            """
        )
        rows = cur.fetchall()
    unprotected = [name for name, enabled in rows if not enabled]
    assert not unprotected, f"tables with user_id and no RLS: {unprotected}"


# --------------------------------------------------------------------------
# deduct_credits
# --------------------------------------------------------------------------


def test_deducting_more_than_the_balance_changes_nothing(db, user_id):
    """The failure must roll back the UPDATE as well as the ledger insert."""
    with db.cursor() as cur:
        cur.execute("SELECT add_credits(%s, %s, 'purchase')", (user_id, 1_000))
    before = balance_of(db, user_id)

    with pytest.raises(Exception, match="Insufficient credits"):
        with db.cursor() as cur:
            cur.execute("SELECT deduct_credits(%s, %s)", (user_id, 5_000))

    assert balance_of(db, user_id) == before
    assert [r for r in ledger_rows(db, user_id) if r[0] == "usage"] == []


def test_spending_the_exact_balance_lands_on_zero(db, user_id):
    with db.cursor() as cur:
        cur.execute("SELECT add_credits(%s, %s, 'purchase')", (user_id, 1_000))
        cur.execute("SELECT deduct_credits(%s, %s)", (user_id, 1_000))
        assert cur.fetchone()[0] == 0
    assert balance_of(db, user_id) == 0


def test_concurrent_deductions_cannot_overdraw(migrated_database, db, user_id):
    """Two committed connections race for a balance only one can have.

    This is what `UPDATE ... WHERE balance >= p_amount` exists for, and it is
    unprovable without real transactions.
    """
    import psycopg

    with db.cursor() as cur:
        cur.execute("SELECT add_credits(%s, %s, 'purchase')", (user_id, 1_000))

    def spend(amount: int) -> str:
        with psycopg.connect(migrated_database, autocommit=True) as conn, conn.cursor() as cur:
            try:
                cur.execute("SELECT deduct_credits(%s, %s)", (user_id, amount))
                return "ok"
            except Exception as exc:
                return "rejected" if "Insufficient credits" in str(exc) else f"error: {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(spend, [600, 600]))

    assert sorted(outcomes) == ["ok", "rejected"], outcomes
    assert balance_of(db, user_id) >= 0
    assert balance_of(db, user_id) == 400


# --------------------------------------------------------------------------
# add_credits
# --------------------------------------------------------------------------


def test_wallet_funding_is_not_a_valid_transaction_type(db, user_id):
    """The bug that made every wallet deposit fail after taking the money.

    `credit_transaction_type` is ('purchase','usage','bonus','refund'); the wallet
    settlement passed 'wallet_funding', so the RPC raised every time.
    """
    with pytest.raises(Exception, match="invalid input value for enum"):
        with db.cursor() as cur:
            cur.execute("SELECT add_credits(%s, %s, 'wallet_funding')", (user_id, 100))


def test_crediting_with_the_same_idempotency_key_credits_once(db, user_id):
    """A redelivered Stripe webhook must not grant the credits again."""
    key = f"stripe:session:{uuid.uuid4()}"
    with db.cursor() as cur:
        cur.execute(
            "SELECT add_credits(%s, %s, 'purchase', NULL, '{}'::jsonb, %s)", (user_id, 5_000, key)
        )
        first = cur.fetchone()[0]
        cur.execute(
            "SELECT add_credits(%s, %s, 'purchase', NULL, '{}'::jsonb, %s)", (user_id, 5_000, key)
        )
        second = cur.fetchone()[0]

    assert first == second == 5_000
    assert len([r for r in ledger_rows(db, user_id) if r[3] == key]) == 1


def test_concurrent_credits_with_one_key_credit_once(migrated_database, db, user_id):
    """Two simultaneous deliveries of the same event.

    A read-then-write check would let both through; the unique index plus
    ON CONFLICT DO NOTHING is what makes it safe.
    """
    import psycopg

    key = f"stripe:session:{uuid.uuid4()}"

    def credit() -> None:
        with psycopg.connect(migrated_database, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT add_credits(%s, %s, 'purchase', NULL, '{}'::jsonb, %s)",
                (user_id, 5_000, key),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: credit(), range(2)))

    assert balance_of(db, user_id) == 5_000, "one payment, one credit"
    assert len([r for r in ledger_rows(db, user_id) if r[3] == key]) == 1


def test_balance_after_is_recorded_on_the_idempotent_path(db, user_id):
    """The ledger row is claimed before the balance moves, so it must be updated."""
    key = f"stripe:session:{uuid.uuid4()}"
    with db.cursor() as cur:
        cur.execute(
            "SELECT add_credits(%s, %s, 'purchase', NULL, '{}'::jsonb, %s)", (user_id, 7_000, key)
        )
    row = next(r for r in ledger_rows(db, user_id) if r[3] == key)
    assert row[2] == 7_000, "balance_after must not be left at its placeholder 0"


def test_a_non_positive_credit_is_refused(db, user_id):
    for amount in (0, -100):
        with pytest.raises(Exception, match="positive amount"):
            with db.cursor() as cur:
                cur.execute("SELECT add_credits(%s, %s, 'purchase')", (user_id, amount))


def test_new_users_receive_the_welcome_bonus_exactly_once(db):
    """The trigger on auth.users, which the app's own get_balance races."""
    new_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, 'x@example.com')", (new_id,))

    assert balance_of(db, new_id) > 0
    bonuses = [r for r in ledger_rows(db, new_id) if r[0] == "bonus"]
    assert len(bonuses) == 1
