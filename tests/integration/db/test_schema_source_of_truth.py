"""Core tables are defined in migrations/, the directory scripts/migrate.py applies."""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[3]
LIVE = REPO / "migrations"

_CREATE_TABLE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_.]+)", re.I)

# Tables the application reads or writes at runtime.
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
