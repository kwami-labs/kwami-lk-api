"""Integration harness: the real migrations against a real Postgres.

The guarantees that matter for money are not in Python. `deduct_credits` is safe
because of `UPDATE ... WHERE balance >= p_amount` inside a single statement;
crediting is idempotent because of a unique index and `ON CONFLICT DO NOTHING`;
tenants are separated by RLS policies. An in-process fake can model those, but it
cannot prove them -- so these tests run against Postgres with
``migrations/*.sql`` applied unchanged.

Point ``TEST_DATABASE_URL`` at a throwaway database, e.g.

    docker run -d --name kwami-test-pg -e POSTGRES_PASSWORD=test \\
        -e POSTGRES_DB=kwami_test -p 55433:5432 postgres:16-alpine
    export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55433/kwami_test

Without it these tests skip, so the default `make test` stays fast and offline.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH_SHIM = REPO_ROOT / "tests" / "sql" / "00_auth_shim.sql"

pytestmark = pytest.mark.integration


def _dsn() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    dsn = _dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not set; skipping integration tests")
    return dsn


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Apply the auth shim and every migration, once per session."""
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(AUTH_SHIM.read_text())

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "migrate.py"), "--dsn", database_url],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"migrations failed:\n{result.stdout}\n{result.stderr}"
    return database_url


@pytest.fixture
def db(migrated_database: str):
    """An autocommit connection with the tables emptied.

    Truncate rather than a wrapping transaction, because several tests need two
    *committed* connections to race each other.
    """
    import psycopg

    with psycopg.connect(migrated_database, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE credit_transactions, credit_usage_logs, user_credits,
                         usage_reports, payment_events, livekit_sessions,
                         user_kwamis, auth.users
                RESTART IDENTITY CASCADE
                """
            )
        yield conn


@pytest.fixture
def user_id(db) -> str:
    """A user with a zeroed balance.

    Creating a row in auth.users fires the `on_auth_user_created_credits`
    trigger, which grants a 500,000 micro-credit welcome bonus. That is real
    behaviour -- and it is asserted in its own test -- but it makes every
    balance assertion relative, so it is cleared here and tests start from zero.
    """
    new_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO auth.users (id, email) VALUES (%s, %s)",
            (new_id, f"{new_id}@example.com"),
        )
        cur.execute(
            "UPDATE user_credits SET balance = 0, lifetime_purchased = 0 WHERE user_id = %s",
            (new_id,),
        )
        cur.execute("DELETE FROM credit_transactions WHERE user_id = %s", (new_id,))
    return new_id
