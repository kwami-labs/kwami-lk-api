"""`migrations/` is the schema; `supabase/migrations/` is not.

The repository carries two migration directories. The docs called the second a
"hosted-project mirror" and documented `supabase db push` as the way to apply
it -- but the two share two tables out of 37, and the second defines none of the
credits, kwami or wallet tables this API reads. Provisioning from it produces a
database the service cannot run against.

This does not force a choice between them. It fails if the *live* schema stops
being the one under `migrations/`, which is the mistake that costs a day.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[3]
LIVE = REPO / "migrations"
LEGACY = REPO / "supabase" / "migrations"

_CREATE_TABLE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_.]+)", re.I)

# Tables the application reads or writes at runtime. Kept explicit: the point is
# that these must come from `migrations/`, whatever else exists.
CORE_TABLES = {
    "user_credits",
    "credit_transactions",
    "credit_usage_logs",
    "user_kwamis",
    "kwami_channels",
    "kwami_wallets",
    "payment_events",
    "usage_reports",
    "livekit_sessions",
}


def _tables(directory: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for sql in directory.glob("*.sql"):
        found |= {t.split(".")[-1].lower() for t in _CREATE_TABLE.findall(sql.read_text())}
    return found


def test_the_live_schema_defines_every_core_table():
    missing = CORE_TABLES - _tables(LIVE)
    assert not missing, f"migrations/ no longer defines: {sorted(missing)}"


def test_no_core_table_comes_only_from_the_legacy_directory():
    """If a table the code needs exists only there, someone has added schema to
    the wrong directory -- and CI applies only `migrations/`."""
    if not LEGACY.exists():
        pytest.skip("the legacy directory has been removed")
    only_legacy = (_tables(LEGACY) & CORE_TABLES) - _tables(LIVE)
    assert not only_legacy, (
        f"these are defined only in supabase/migrations/: {sorted(only_legacy)}. "
        "Add them to migrations/ -- that is the directory scripts/migrate.py applies."
    )


def test_the_legacy_directory_is_labelled():
    """A future reader must not mistake it for the live schema again."""
    if not LEGACY.exists():
        pytest.skip("the legacy directory has been removed")
    readme = LEGACY / "README.md"
    assert readme.is_file(), "supabase/migrations/ needs a README saying what it is"
    assert "not this service's schema" in readme.read_text()
