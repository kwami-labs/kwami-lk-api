"""`/memory` — debug, facts, delete, messages, and the edge/node deletes.

Every route starts with `verify_user_access`, so the 403 is asserted once per
route rather than per case. The interesting behaviour is what happens when Zep
fails: most routes collect the error rather than propagating it, because a
partial answer is more useful than none — and `/facts` treats a 404 as "no
memory yet" rather than an error.
"""

from __future__ import annotations

import pytest

from tests.unit.api.memory.conftest import USER_ID, edge, node, thread

pytestmark = pytest.mark.anyio


class TestDebug:
    PATH = f"/memory/debug/{USER_ID}"

    async def test_it_reports_facts_nodes_and_threads(self, mem, zep):
        zep.graph.search.result = type("R", (), {"edges": [edge(fact="likes pizza")]})()
        zep.graph.node.get_by_user_id.result = [node(name="Ada")]
        zep.thread.list_all.result = type("R", (), {"threads": [thread("t-1")], "total_count": 1})()

        body = (await mem.get(self.PATH)).json()

        assert body["user_id"] == USER_ID
        assert body["facts"] == ["likes pizza"]
        assert body["graph_edges"][0]["name"] == "RELATES_TO"
        assert body["graph_nodes"] == 1
        assert body["node_names"] == ["Ada"]
        assert body["threads"][0]["thread_id"] == "t-1"
        assert body["threads"][0]["context"] == "ctx"
        assert body["errors"] == []

    async def test_another_users_memory_is_a_403(self, mem):
        assert (await mem.get("/memory/debug/someone-else")).status_code == 403

    async def test_a_search_failure_is_collected_not_raised(self, mem, zep):
        zep.graph.search.error = RuntimeError("zep unavailable")
        body = (await mem.get(self.PATH)).json()
        assert any("graph.search" in e for e in body["errors"])
        assert body["facts"] == []

    async def test_a_node_failure_is_collected(self, mem, zep):
        zep.graph.node.get_by_user_id.error = RuntimeError("nodes down")
        body = (await mem.get(self.PATH)).json()
        assert any("graph.node" in e for e in body["errors"])

    async def test_a_thread_listing_failure_is_collected(self, mem, zep):
        zep.thread.list_all.error = RuntimeError("threads down")
        body = (await mem.get(self.PATH)).json()
        assert any("thread.list_all" in e for e in body["errors"])

    async def test_a_context_failure_is_recorded_on_the_thread(self, mem, zep):
        zep.thread.list_all.result = type("R", (), {"threads": [thread("t-1")], "total_count": 1})()
        zep.thread.get_context.error = RuntimeError("context unavailable")
        body = (await mem.get(self.PATH)).json()
        assert "context_error" in body["threads"][0]

    async def test_another_tenants_threads_are_not_listed(self, mem, zep):
        """This endpoint used to return every thread in the project."""
        zep.thread.list_all.result = type(
            "R", (), {"threads": [thread("t-other", user_id="someone-else")], "total_count": 1}
        )()
        assert (await mem.get(self.PATH)).json()["threads"] == []

    async def test_a_long_context_is_truncated(self, mem, zep):
        zep.thread.list_all.result = type("R", (), {"threads": [thread("t-1")], "total_count": 1})()
        zep.thread.get_context.result = type("C", (), {"context": "x" * 600})()
        context = (await mem.get(self.PATH)).json()["threads"][0]["context"]
        assert context.endswith("...")
        assert len(context) == 503

    async def test_an_empty_context_is_omitted(self, mem, zep):
        zep.thread.list_all.result = type("R", (), {"threads": [thread("t-1")], "total_count": 1})()
        zep.thread.get_context.result = type("C", (), {"context": ""})()
        assert "context" not in (await mem.get(self.PATH)).json()["threads"][0]

    async def test_an_edge_without_a_fact_is_skipped(self, mem, zep):
        bare = edge()
        del bare.fact
        zep.graph.search.result = type("R", (), {"edges": [bare]})()
        body = (await mem.get(self.PATH)).json()
        assert body["facts"] == []
        assert body["graph_edges"] == []

    async def test_an_empty_search_response(self, mem, zep):
        zep.graph.search.result = None
        assert (await mem.get(self.PATH)).json()["facts"] == []

    async def test_no_nodes_reports_zero(self, mem, zep):
        zep.graph.node.get_by_user_id.result = None
        body = (await mem.get(self.PATH)).json()
        assert body["graph_nodes"] == 0
        assert "node_names" not in body


class TestFacts:
    PATH = f"/memory/{USER_ID}/facts"

    async def test_it_returns_paginated_facts(self, mem, zep):
        zep.graph.search.result = type(
            "R", (), {"edges": [edge(fact=f"fact {i}") for i in range(5)]}
        )()
        body = (await mem.get(self.PATH, params={"limit": 2, "offset": 1})).json()
        assert body["facts"] == ["fact 1", "fact 2"]
        assert body["count"] == 2
        assert body["total"] == 5
        assert body["offset"] == 1
        assert body["has_more"] is True

    async def test_the_last_page_reports_no_more(self, mem, zep):
        zep.graph.search.result = type("R", (), {"edges": [edge(fact=f"f{i}") for i in range(3)]})()
        body = (await mem.get(self.PATH, params={"limit": 2, "offset": 2})).json()
        assert body["has_more"] is False
        assert body["count"] == 1

    async def test_an_empty_graph(self, mem, zep):
        body = (await mem.get(self.PATH)).json()
        assert body == {
            "facts": [],
            "count": 0,
            "total": 0,
            "offset": 0,
            "limit": 50,
            "has_more": False,
        }

    async def test_edges_without_a_fact_are_dropped(self, mem, zep):
        zep.graph.search.result = type("R", (), {"edges": [edge(fact=None), edge(fact="kept")]})()
        assert (await mem.get(self.PATH)).json()["facts"] == ["kept"]

    async def test_a_404_from_zep_reads_as_no_memory(self, mem, zep):
        """A user with no graph yet is not an error."""
        zep.graph.search.error = RuntimeError("status 404: user not found")
        body = (await mem.get(self.PATH)).json()
        assert body["facts"] == []
        assert body["total"] == 0

    async def test_any_other_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.search.error = RuntimeError("zep exploded")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.get(self.PATH)
        assert r.status_code == 500
        assert "Failed to fetch facts" in caplog.text

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
    async def test_out_of_range_pagination_is_a_422(self, mem, params):
        assert (await mem.get(self.PATH, params=params)).status_code == 422

    async def test_another_users_facts_are_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/facts")).status_code == 403


class TestDeleteUserMemory:
    PATH = f"/memory/{USER_ID}"

    async def test_it_deletes_owned_threads_and_the_user(self, mem, zep):
        zep.thread.list_all.result = type(
            "R", (), {"threads": [thread("t-1"), thread("t-2")], "total_count": 2}
        )()
        body = (await mem.delete(self.PATH)).json()
        assert body["success"] is True
        assert body["deleted_threads"] == 2
        assert body["deleted_user"] is True
        assert body["errors"] is None
        assert len(zep.thread.delete.calls) == 2

    async def test_another_tenants_threads_are_left_alone(self, mem, zep):
        """The substring ownership test used to delete other tenants' threads."""
        zep.thread.list_all.result = type(
            "R", (), {"threads": [thread("t-other", user_id="someone-else")], "total_count": 1}
        )()
        body = (await mem.delete(self.PATH)).json()
        assert body["deleted_threads"] == 0
        assert not zep.thread.delete.called

    async def test_a_failing_thread_delete_is_collected_and_the_rest_proceed(self, mem, zep):
        zep.thread.list_all.result = type("R", (), {"threads": [thread("t-1")], "total_count": 1})()
        zep.thread.delete.error = RuntimeError("thread locked")
        body = (await mem.delete(self.PATH)).json()
        assert body["deleted_threads"] == 0
        assert any("Failed to delete thread t-1" in e for e in body["errors"])
        assert body["deleted_user"] is True

    async def test_a_failing_thread_listing_is_collected(self, mem, zep):
        zep.thread.list_all.error = RuntimeError("threads down")
        body = (await mem.delete(self.PATH)).json()
        assert any("Failed to list threads" in e for e in body["errors"])

    async def test_a_404_on_the_user_is_reported_as_not_found(self, mem, zep):
        zep.user.delete.error = RuntimeError("status 404")
        body = (await mem.delete(self.PATH)).json()
        assert body["deleted_user"] is False
        assert any("not found" in e for e in body["errors"])
        assert body["success"] is False

    async def test_another_user_delete_failure_is_collected(self, mem, zep):
        zep.user.delete.error = RuntimeError("permission denied")
        body = (await mem.delete(self.PATH)).json()
        assert any("Failed to delete user" in e for e in body["errors"])

    async def test_success_is_true_if_any_thread_was_deleted(self, mem, zep):
        zep.thread.list_all.result = type("R", (), {"threads": [thread("t-1")], "total_count": 1})()
        zep.user.delete.error = RuntimeError("status 404")
        assert (await mem.delete(self.PATH)).json()["success"] is True

    async def test_another_users_memory_is_a_403(self, mem):
        assert (await mem.delete("/memory/someone-else")).status_code == 403


class TestDeleteEdgeAndNode:
    async def test_deleting_an_edge(self, mem, zep):
        r = await mem.delete(f"/memory/{USER_ID}/edge/e-1")
        assert r.status_code == 200
        assert zep.graph.edge.delete.last["uuid_"] == "e-1"

    async def test_deleting_a_node(self, mem, zep):
        r = await mem.delete(f"/memory/{USER_ID}/node/n-1")
        assert r.status_code == 200
        assert zep.graph.node.delete.last["uuid_"] == "n-1"

    async def test_an_edge_delete_failure_is_a_500(self, mem, zep):
        zep.graph.edge.delete.error = RuntimeError("zep down")
        assert (await mem.delete(f"/memory/{USER_ID}/edge/e-1")).status_code == 500

    async def test_a_node_delete_failure_is_a_500(self, mem, zep):
        zep.graph.node.delete.error = RuntimeError("zep down")
        assert (await mem.delete(f"/memory/{USER_ID}/node/n-1")).status_code == 500

    async def test_another_users_edge_is_a_403(self, mem, zep):
        r = await mem.delete("/memory/someone-else/edge/e-1")
        assert r.status_code == 403
        assert not zep.graph.edge.delete.called

    async def test_another_users_node_is_a_403(self, mem, zep):
        r = await mem.delete("/memory/someone-else/node/n-1")
        assert r.status_code == 403
        assert not zep.graph.node.delete.called


class TestOuterErrorHandlers:
    """The outer `except` on each route, reached by failing something the inner
    handlers do not wrap. Each turns an unexpected failure into a 500 rather than
    letting it reach the app's catch-all."""

    async def test_delete_user_memory(self, monkeypatch, mem, zep, caplog):
        from src.api.routes import memory as mem_mod

        def boom(*a, **k):
            raise RuntimeError("iterator exploded")

        monkeypatch.setattr(mem_mod, "iter_all_threads", boom)
        monkeypatch.setattr(
            mem_mod, "thread_belongs_to", lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
        )
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.delete(f"/memory/{USER_ID}")
        # The inner handlers absorb the listing failure, so this still answers 200.
        assert r.status_code == 200

    async def test_messages_outer_handler(self, monkeypatch, mem, zep, caplog):
        """The sort and the slice sit outside the inner try/except."""
        from src.api.routes import memory as mem_mod

        def boom(*a, **k):
            raise RuntimeError("len exploded")

        monkeypatch.setattr(mem_mod, "len", boom, raising=False)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.get(f"/memory/{USER_ID}/messages")
        assert r.status_code == 500
        assert "Failed to fetch messages" in caplog.text

    @pytest.mark.parametrize(
        ("path", "message"),
        [
            (f"/memory/{USER_ID}/edges", "Failed to fetch edges"),
            (f"/memory/{USER_ID}/nodes", "Failed to fetch nodes"),
        ],
    )
    async def test_list_routes_outer_handler(self, monkeypatch, mem, zep, caplog, path, message):
        """`offset`/`limit` slicing is outside the inner try, so a bad slice lands here."""
        from src.api.routes import memory as mem_mod

        def boom(*a, **k):
            raise RuntimeError("len exploded")

        monkeypatch.setattr(mem_mod, "len", boom, raising=False)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.get(path)
        assert r.status_code == 500
        assert message in caplog.text

    async def test_search_outer_handler(self, monkeypatch, mem, zep, caplog):
        from src.api.routes import memory as mem_mod

        def boom(*a, **k):
            raise RuntimeError("len exploded")

        monkeypatch.setattr(mem_mod, "len", boom, raising=False)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.get(f"/memory/{USER_ID}/search", params={"q": "x"})
        assert r.status_code == 500
        assert "Failed to search graph" in caplog.text

    @pytest.mark.parametrize(
        ("uuid_path", "attr", "message"),
        [
            ("edge/e-1", "edge", "Failed to delete edge"),
            ("node/n-1", "node", "Failed to delete node"),
        ],
    )
    async def test_a_404_from_a_delete_is_surfaced(self, mem, zep, uuid_path, attr, message):
        getattr(zep.graph, attr).delete.error = RuntimeError("status 404 not found")
        r = await mem.delete(f"/memory/{USER_ID}/{uuid_path}")
        assert r.status_code == 404
