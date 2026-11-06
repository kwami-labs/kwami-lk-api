"""Walking a user graph past Zep's 50-record page.

The fake listing behaves like Zep: at most 50 records per call, continued after
the record named by `uuid_cursor`. A graph of 120 facts therefore takes three
pages plus the empty page that ends the walk.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.memory.paging import ZEP_PAGE_SIZE, collect, page, uuid_of, walk
from tests.unit.api.memory.conftest import USER_ID, FakeZep, edge

pytestmark = pytest.mark.anyio


def _graph(zep: FakeZep, count: int) -> list:
    records = [edge(uuid=f"e-{i:03d}") for i in range(count)]
    zep.graph.edge.get_by_user_id.result = records
    return records


class TestUuidOf:
    def test_it_prefers_the_sdk_attribute(self):
        assert uuid_of(SimpleNamespace(uuid_="a", uuid="b")) == "a"

    def test_it_falls_back_to_the_plain_attribute(self):
        assert uuid_of(SimpleNamespace(uuid="b")) == "b"

    def test_a_record_without_one_is_none(self):
        assert uuid_of(SimpleNamespace()) is None


class TestWalk:
    async def test_it_reads_past_the_first_page(self):
        zep = FakeZep()
        records = _graph(zep, 120)
        got = await collect(zep.graph.edge.get_by_user_id, USER_ID)
        assert [uuid_of(r) for r in got] == [uuid_of(r) for r in records]

    async def test_each_call_asks_for_a_full_page_and_follows_the_cursor(self):
        zep = FakeZep()
        _graph(zep, 120)
        await collect(zep.graph.edge.get_by_user_id, USER_ID)
        calls = zep.graph.edge.get_by_user_id.calls
        assert [c.get("uuid_cursor") for c in calls] == [None, "e-049", "e-099", "e-119"]
        assert all(c["limit"] == ZEP_PAGE_SIZE and c["user_id"] == USER_ID for c in calls)

    async def test_a_short_page_does_not_end_the_walk(self):
        """A page shorter than asked for is not proof of the end: only an empty one is."""
        calls = []

        async def fetch(**kwargs):
            calls.append(kwargs)
            return [] if len(calls) == 3 else [edge(uuid=f"e-{len(calls)}")]

        got = await collect(fetch, USER_ID)
        assert [uuid_of(r) for r in got] == ["e-1", "e-2"]

    async def test_it_stops_at_max_items(self):
        zep = FakeZep()
        _graph(zep, 120)
        got = await collect(zep.graph.edge.get_by_user_id, USER_ID, max_items=60)
        assert len(got) == 60
        assert len(zep.graph.edge.get_by_user_id.calls) == 2

    async def test_a_cursor_that_does_not_move_ends_the_walk(self):
        """A listing that ignored the cursor would otherwise loop forever."""

        async def fetch(**kwargs):
            return [edge(uuid="same")]

        assert len(await collect(fetch, USER_ID)) == 1

    async def test_a_record_without_a_uuid_ends_the_walk(self):
        async def fetch(**kwargs):
            return [SimpleNamespace(fact="no id")]

        assert len(await collect(fetch, USER_ID)) == 1

    async def test_a_none_page_is_empty(self):
        async def fetch(**kwargs):
            return None

        assert await collect(fetch, USER_ID) == []

    async def test_options_reach_the_listing(self):
        zep = FakeZep()
        _graph(zep, 3)
        await collect(zep.graph.edge.get_by_user_id, USER_ID, order_by="degree")
        assert zep.graph.edge.get_by_user_id.calls[0]["order_by"] == "degree"

    async def test_a_starting_cursor_skips_what_came_before(self):
        zep = FakeZep()
        _graph(zep, 5)
        got = [r async for r in walk(zep.graph.edge.get_by_user_id, USER_ID, cursor="e-002")]
        assert [uuid_of(r) for r in got] == ["e-003", "e-004"]


class TestPage:
    async def test_a_middle_page_returns_a_cursor(self):
        zep = FakeZep()
        _graph(zep, 120)
        records, cursor = await page(zep.graph.edge.get_by_user_id, USER_ID, limit=100)
        assert len(records) == 100
        assert cursor == "e-099"

    async def test_the_cursor_continues_where_the_page_ended(self):
        zep = FakeZep()
        _graph(zep, 120)
        records, cursor = await page(
            zep.graph.edge.get_by_user_id, USER_ID, limit=100, cursor="e-099"
        )
        assert [uuid_of(r) for r in records] == [f"e-{i}" for i in range(100, 120)]
        assert cursor is None

    async def test_an_offset_is_walked_and_dropped(self):
        zep = FakeZep()
        _graph(zep, 120)
        records, cursor = await page(zep.graph.edge.get_by_user_id, USER_ID, limit=10, offset=55)
        assert [uuid_of(r) for r in records] == [f"e-0{i}" for i in range(55, 65)]
        assert cursor == "e-064"

    async def test_the_last_page_has_no_cursor(self):
        zep = FakeZep()
        _graph(zep, 10)
        records, cursor = await page(zep.graph.edge.get_by_user_id, USER_ID, limit=10)
        assert len(records) == 10
        assert cursor is None

    async def test_an_offset_past_the_end_is_empty(self):
        zep = FakeZep()
        _graph(zep, 10)
        assert await page(zep.graph.edge.get_by_user_id, USER_ID, limit=10, offset=50) == (
            [],
            None,
        )
