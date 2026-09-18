"""Losing a browser context handle signs the user out of everything.

The navigation panel runs a cloud browser carrying the user's real cookies and
logins. Browserbase addresses that state by an opaque Context id returned once
at creation, with no lookup-by-name endpoint -- so this table is the only way
back to it. A lost row is not a cache miss: the user is signed out of every
site, and the old context is orphaned, still stored and still billed.

That makes three things load-bearing, and each is tested here: the row is
upserted rather than inserted (so a race cannot leave two half-signed-in
profiles), unknown vendors are rejected before they reach the CHECK constraint,
and `owner_key` is accepted in both the shapes the agent actually sends.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.services import browser_sessions
from src.services.browser_sessions import (
    UnsupportedVendorError,
    delete_browser_context,
    get_browser_context,
    save_browser_context,
)

KWAMI_UUID = "3f1a7b2c-9d4e-4f55-8a11-2b3c4d5e6f70"
TELEPHONY_IDENTITY = "sip_+14155552671"


class FakeQuery:
    """Records the query the service built, and replays a canned result."""

    def __init__(self, table: "FakeTable", op: str) -> None:
        self.table = table
        self.op = op
        self.filters: dict[str, Any] = {}
        self.payload: Any = None
        self.on_conflict: str | None = None

    def select(self, *_columns: str) -> "FakeQuery":
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self.filters[column] = value
        return self

    def limit(self, _n: int) -> "FakeQuery":
        return self

    def execute(self) -> Any:
        self.table.calls.append(self)
        return type("Result", (), {"data": self.table.result})()


class FakeTable:
    def __init__(self, name: str, result: Any) -> None:
        self.name = name
        self.result = result
        self.calls: list[FakeQuery] = []

    def select(self, *_columns: str) -> FakeQuery:
        return FakeQuery(self, "select")

    def upsert(self, payload: Any, on_conflict: str | None = None) -> FakeQuery:
        query = FakeQuery(self, "upsert")
        query.payload = payload
        query.on_conflict = on_conflict
        return query

    def delete(self) -> FakeQuery:
        return FakeQuery(self, "delete")


class FakeSupabase:
    def __init__(self, result: Any = None) -> None:
        self.tables: dict[str, FakeTable] = {}
        self._result = result if result is not None else []

    def table(self, name: str) -> FakeTable:
        return self.tables.setdefault(name, FakeTable(name, self._result))


@pytest.fixture
def supabase(monkeypatch):
    def _install(result: Any = None) -> FakeSupabase:
        fake = FakeSupabase(result)
        monkeypatch.setattr(browser_sessions, "get_supabase_admin", lambda: fake)
        return fake

    return _install


# -- Reading -----------------------------------------------------------------


def test_reads_the_handle_for_an_owner_and_vendor(supabase):
    fake = supabase([{"context_id": "ctx-7"}])

    assert get_browser_context(KWAMI_UUID, "browserbase") == "ctx-7"

    (query,) = fake.tables["browser_contexts"].calls
    assert query.filters == {"owner_key": KWAMI_UUID, "vendor": "browserbase"}


def test_an_owner_with_no_saved_profile_reads_as_none(supabase):
    supabase([])
    assert get_browser_context(KWAMI_UUID, "browserbase") is None


def test_the_vendor_is_matched_case_insensitively(supabase):
    fake = supabase([{"context_id": "ctx-7"}])

    get_browser_context(KWAMI_UUID, "  BrowserBase ")

    (query,) = fake.tables["browser_contexts"].calls
    assert query.filters["vendor"] == "browserbase"


# -- Writing -----------------------------------------------------------------


def test_the_row_is_upserted_on_owner_and_vendor(supabase):
    """Two sessions starting at once must not leave two profiles for one user."""
    fake = supabase([{"context_id": "ctx-7"}])

    save_browser_context(KWAMI_UUID, "browserbase", "ctx-7")

    (query,) = fake.tables["browser_contexts"].calls
    assert query.op == "upsert"
    assert query.on_conflict == "owner_key,vendor"
    assert query.payload["context_id"] == "ctx-7"


def test_timestamps_are_sent_explicitly(supabase):
    """An upsert that resolves to an UPDATE never re-runs a column default."""
    fake = supabase([{}])

    save_browser_context(KWAMI_UUID, "browserbase", "ctx-7")

    payload = fake.tables["browser_contexts"].calls[0].payload
    assert payload["updated_at"] and payload["last_used_at"]
    # A literal "now()" would reach Postgres as a string it cannot parse.
    assert "now()" not in str(payload["updated_at"])


def test_a_uuid_owner_is_linked_to_the_user_for_self_service_deletion(supabase):
    fake = supabase([{}])

    save_browser_context(KWAMI_UUID, "browserbase", "ctx-7")

    assert fake.tables["browser_contexts"].calls[0].payload["user_id"] == KWAMI_UUID


def test_a_telephony_identity_owner_stores_no_user_id(supabase):
    """`kwami_id` falls back to the participant identity, which is not a uuid.

    A foreign key here would reject the row and take the browser down with it.
    """
    fake = supabase([{}])

    save_browser_context(TELEPHONY_IDENTITY, "browserbase", "ctx-7")

    payload = fake.tables["browser_contexts"].calls[0].payload
    assert "user_id" not in payload
    assert payload["owner_key"] == TELEPHONY_IDENTITY


# -- Rejected input ----------------------------------------------------------


@pytest.mark.parametrize("vendor", ["", "chrome", "playwright", "browserbase-eu"])
def test_unknown_vendors_are_rejected_before_the_database(supabase, vendor: str):
    """Otherwise the CHECK constraint turns a client mistake into a 500."""
    fake = supabase([])

    with pytest.raises(UnsupportedVendorError):
        save_browser_context(KWAMI_UUID, vendor, "ctx-7")

    assert fake.tables.get("browser_contexts") is None or not fake.tables["browser_contexts"].calls


@pytest.mark.parametrize("owner", ["", "   "])
def test_a_blank_owner_is_rejected(supabase, owner: str):
    """A blank key would collide every anonymous session onto one profile."""
    supabase([])
    with pytest.raises(ValueError):
        save_browser_context(owner, "browserbase", "ctx-7")
    with pytest.raises(ValueError):
        get_browser_context(owner, "browserbase")


@pytest.mark.parametrize("context_id", ["", "   "])
def test_a_blank_handle_is_rejected(supabase, context_id: str):
    supabase([])
    with pytest.raises(ValueError):
        save_browser_context(KWAMI_UUID, "browserbase", context_id)


def test_oversized_values_are_rejected(supabase):
    supabase([])
    with pytest.raises(ValueError):
        save_browser_context(KWAMI_UUID, "browserbase", "c" * 300)
    with pytest.raises(ValueError):
        save_browser_context("o" * 300, "browserbase", "ctx-7")


# -- Deleting ----------------------------------------------------------------


def test_deleting_without_a_vendor_clears_every_profile(supabase):
    fake = supabase([{"id": "1"}, {"id": "2"}])

    assert delete_browser_context(KWAMI_UUID) == 2

    (query,) = fake.tables["browser_contexts"].calls
    assert query.filters == {"owner_key": KWAMI_UUID}


def test_deleting_with_a_vendor_clears_only_that_one(supabase):
    fake = supabase([{"id": "1"}])

    assert delete_browser_context(KWAMI_UUID, "browser_use") == 1

    (query,) = fake.tables["browser_contexts"].calls
    assert query.filters == {"owner_key": KWAMI_UUID, "vendor": "browser_use"}
