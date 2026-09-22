"""Does `tests/fakes/supabase.py` actually behave like the database?

`tests/fakes/supabase.py` opens by promising that "every method implemented here
is covered by tests/integration/contracts/test_fake_supabase_conformance.py,
which runs the same assertions against this fake and against a real PostgREST.
Behaviour the fake claims but PostgREST does not is a CI failure." That file did
not exist. The fake stood in for the database in 2,200 tests on an unchecked
promise.

This is that file. Each scenario is written once against the builder surface and
run three ways -- the sync fake, the async fake, and `PgClient`, which issues the
SQL PostgREST would -- and the results are compared.

Running it against both fake modes is the point of the exercise: `async_mode` was
dead code, and the async SDK migration turns it on for the whole suite at once.

Rows are compared on the columns a caller can predict. Server-side values
(`id`, `created_at`) are not comparable across two databases, and their *shape*
is asserted separately.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from postgrest.exceptions import APIError

from tests.fakes.supabase import FakeDatabase, FakeSupabase
from tests.integration.contracts.pg_client import PgClient

pytestmark = pytest.mark.integration

TABLE = "user_kwamis"

# Three kwamis with predictable, orderable values; `config` carries a JSON member
# so the `col->>key` accessor has something to read.
SEED = [
    {"name": "alpha", "emoji": "A", "config": {"scene": "forest", "rank": 1}},
    {"name": "beta", "emoji": "B", "config": {"scene": "ocean", "rank": 2}},
    {"name": "gamma", "emoji": "C", "config": {"scene": "forest", "rank": 3}},
]


def _scalar(value: Any) -> Any:
    """Collapse the type differences between psycopg and a dict of Python values."""
    if isinstance(value, uuid.UUID | datetime | date | Decimal):
        return str(value)
    return value


def _comparable(result: Any) -> Any:
    """The part of a result two different databases can be expected to agree on."""
    data = result.data
    if data is None:
        return None
    rows = data if isinstance(data, list) else [data]
    out = [
        {k: _scalar(v) for k, v in row.items() if k in {"name", "emoji", "config"}} for row in rows
    ]
    return out if isinstance(data, list) else out[0]


def _outcome(client, scenario) -> Any:
    """Run a scenario, capturing an APIError's code rather than letting it escape."""
    try:
        return ("ok", _comparable(scenario(client)))
    except APIError as exc:
        return ("error", (exc.code or "").strip())
    except NotImplementedError:
        return ("unimplemented", None)


# Each scenario is a single builder chain. Anything the fake implements belongs
# here; anything it does not is not a divergence a caller can reach.
SCENARIOS: dict[str, Any] = {
    "select_all": lambda c: c.table(TABLE).select("*").execute(),
    "select_columns": lambda c: c.table(TABLE).select("name,emoji").execute(),
    "eq": lambda c: c.table(TABLE).select("*").eq("name", "beta").execute(),
    "eq_no_match": lambda c: c.table(TABLE).select("*").eq("name", "nope").execute(),
    "neq": lambda c: c.table(TABLE).select("*").neq("name", "beta").order("name").execute(),
    "gt": lambda c: c.table(TABLE).select("*").gt("name", "alpha").order("name").execute(),
    "gte": lambda c: c.table(TABLE).select("*").gte("name", "beta").order("name").execute(),
    "lt": lambda c: c.table(TABLE).select("*").lt("name", "gamma").order("name").execute(),
    "lte": lambda c: c.table(TABLE).select("*").lte("name", "beta").order("name").execute(),
    "in_": lambda c: (
        c.table(TABLE).select("*").in_("name", ["alpha", "gamma"]).order("name").execute()
    ),
    "in_empty": lambda c: c.table(TABLE).select("*").in_("name", []).execute(),
    "like_substring": lambda c: c.table(TABLE).select("*").like("name", "%et%").execute(),
    "like_prefix": lambda c: c.table(TABLE).select("*").like("name", "al%").execute(),
    "like_is_case_sensitive": lambda c: (
        c.table(TABLE).select("*").like("name", "%ALPHA%").execute()
    ),
    "ilike": lambda c: c.table(TABLE).select("*").ilike("name", "%ALPHA%").execute(),
    "or_": lambda c: (
        c.table(TABLE).select("*").or_("name.eq.alpha,name.eq.gamma").order("name").execute()
    ),
    "order_asc": lambda c: c.table(TABLE).select("*").order("name").execute(),
    "order_desc": lambda c: c.table(TABLE).select("*").order("name", desc=True).execute(),
    "range": lambda c: c.table(TABLE).select("*").order("name").range(0, 1).execute(),
    "range_beyond_end": lambda c: c.table(TABLE).select("*").order("name").range(5, 9).execute(),
    "limit": lambda c: c.table(TABLE).select("*").order("name").limit(2).execute(),
    "single_one_row": lambda c: c.table(TABLE).select("*").eq("name", "beta").single().execute(),
    "single_no_rows": lambda c: c.table(TABLE).select("*").eq("name", "nope").single().execute(),
    "single_many_rows": lambda c: c.table(TABLE).select("*").single().execute(),
    "maybe_single_one_row": lambda c: (
        c.table(TABLE).select("*").eq("name", "beta").maybe_single().execute()
    ),
    "maybe_single_no_rows": lambda c: (
        c.table(TABLE).select("*").eq("name", "nope").maybe_single().execute()
    ),
    "maybe_single_many_rows": lambda c: c.table(TABLE).select("*").maybe_single().execute(),
    "json_accessor": lambda c: (
        c.table(TABLE).select("*").eq("config->>scene", "forest").order("name").execute()
    ),
    "json_accessor_numeric_as_text": lambda c: (
        c.table(TABLE).select("*").eq("config->>rank", "2").execute()
    ),
    "update_one": lambda c: c.table(TABLE).update({"emoji": "Z"}).eq("name", "beta").execute(),
    "update_no_match": lambda c: c.table(TABLE).update({"emoji": "Z"}).eq("name", "nope").execute(),
    "delete_one": lambda c: c.table(TABLE).delete().eq("name", "beta").execute(),
    "delete_no_match": lambda c: c.table(TABLE).delete().eq("name", "nope").execute(),
}


@pytest.fixture
def pg(db, user_id):
    """A PgClient over the migrated database, seeded with SEED."""
    import json

    with db.cursor() as cur:
        for row in SEED:
            cur.execute(
                "INSERT INTO user_kwamis (user_id, name, emoji, config) VALUES (%s,%s,%s,%s)",
                (user_id, row["name"], row["emoji"], json.dumps(row["config"])),
            )
    return PgClient(db)


@pytest.fixture
def fake_sync(user_id):
    return _seeded_fake(user_id, async_mode=False)


def _seeded_fake(user_id: str, *, async_mode: bool) -> FakeSupabase:
    database = FakeDatabase()
    client = FakeSupabase(database, async_mode=async_mode)
    for row in SEED:
        database.seed(TABLE, {"user_id": user_id, **row})
    return client


@pytest.mark.parametrize("scenario_name", sorted(SCENARIOS))
def test_the_fake_matches_postgres(scenario_name, pg, fake_sync):
    """The fake's answer and the database's answer, for one builder chain."""
    scenario = SCENARIOS[scenario_name]
    assert _outcome(fake_sync, scenario) == _outcome(pg, scenario), scenario_name


@pytest.mark.anyio
@pytest.mark.parametrize("scenario_name", sorted(SCENARIOS))
async def test_async_mode_matches_sync_mode(scenario_name, user_id):
    """`async_mode=True` was never exercised anywhere, and the async SDK migration
    switches the whole suite onto it. The two modes must not disagree."""
    scenario = SCENARIOS[scenario_name]
    sync_client = _seeded_fake(user_id, async_mode=False)
    async_client = _seeded_fake(user_id, async_mode=True)

    sync_outcome = _outcome(sync_client, scenario)

    try:
        async_outcome = ("ok", _comparable(await scenario(async_client)))
    except APIError as exc:
        async_outcome = ("error", (exc.code or "").strip())
    except NotImplementedError:
        async_outcome = ("unimplemented", None)

    assert sync_outcome == async_outcome, scenario_name


def test_declared_unique_indexes_exist_in_the_database(migrated_database):
    """Every constraint the fake enforces must be one the database also enforces.

    `DEFAULT_UNIQUE_INDEXES` is a hand-copied list of (table, columns). Two entries
    named a column that does not exist -- `provider_sid`, taken from the tail of the
    index name, where the columns are `provider_call_sid` and `provider_message_sid`.
    A unique constraint on a non-existent column silently never fires, so duplicate
    Twilio webhooks inserted cleanly in every test and would have been rejected by
    Postgres.

    Comparing against `pg_indexes` rather than re-reading the migrations: the
    database is the authority, and it is already running here.
    """
    import psycopg

    from tests.fakes.supabase import DEFAULT_UNIQUE_INDEXES

    with psycopg.connect(migrated_database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.relname,
                   array_agg(a.attname ORDER BY a.attname)
              FROM pg_index i
              JOIN pg_class t ON t.oid = i.indrelid
              JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
             WHERE i.indisunique
               AND t.relnamespace = 'public'::regnamespace
             GROUP BY t.relname, i.indexrelid
            """
        )
        actual = {(table, tuple(sorted(columns))) for table, columns in cur.fetchall()}

    declared = {(table, tuple(sorted(columns))) for table, columns in DEFAULT_UNIQUE_INDEXES}
    missing = declared - actual
    assert not missing, (
        f"the fake enforces unique constraints the database does not have: {sorted(missing)}"
    )


class TestServerSideValues:
    """What the database fills in, which the fake has to fill in too."""

    def test_an_inserted_row_gets_a_uuid_id_and_timestamps(self, pg, fake_sync, user_id):
        payload = {"user_id": user_id, "name": "delta", "emoji": "D"}
        for client in (fake_sync, pg):
            row = client.table(TABLE).insert(dict(payload)).execute().data[0]
            assert uuid.UUID(str(row["id"]))
            assert row["created_at"] is not None
            assert row["updated_at"] is not None

    def test_the_database_applies_column_defaults(self, pg, user_id):
        """The behaviour the fake is measured against."""
        row = pg.table(TABLE).insert({"user_id": user_id}).execute().data[0]
        assert row["name"] == "Kwami"
        assert row["emoji"] == "🌸"
        assert row["config"] == {}

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN GAP: the fake models server-generated ids, created_at/updated_at "
            "and GENERATED columns, but not ordinary column DEFAULTs -- there are 166 "
            "of them across migrations/, and a hand-copied registry would rot against "
            "the SQL silently. A test that inserts a partial row and then reads a "
            "defaulted column back will see None here and a value in Postgres. "
            "Promote such a test to the integration lane. If the fake ever learns "
            "defaults, this xfail turns red (xfail_strict) and should be deleted."
        ),
    )
    def test_the_fake_does_not_apply_column_defaults(self, fake_sync, user_id):
        row = fake_sync.table(TABLE).insert({"user_id": user_id}).execute().data[0]
        assert row.get("name") == "Kwami"
