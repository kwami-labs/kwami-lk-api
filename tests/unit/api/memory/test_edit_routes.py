"""`/memory` — messages, and the edge/node edit routes.

Zep v3 has no in-place edge or node update, so both PATCH routes delete and
recreate through `add_fact_triple`. That makes the merge of old and new fields
the whole behaviour: a field the caller omits has to survive the round trip, and
a node's edges have to be rebuilt on the correct side of the triple.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.unit.api.memory.conftest import USER_ID, edge, node, thread

pytestmark = pytest.mark.anyio


def message(uuid="m-1", content="hello", role="user", created_at="2026-03-01T00:00:00Z", **extra):
    return SimpleNamespace(
        uuid=uuid,
        content=content,
        role=role,
        role_type=extra.pop("role_type", "user"),
        created_at=created_at,
        **extra,
    )


class TestMessages:
    PATH = f"/memory/{USER_ID}/messages"

    def _threads(self, zep, *threads, total=None):
        zep.thread.list_all.result = SimpleNamespace(
            threads=list(threads), total_count=total if total is not None else len(threads)
        )

    async def test_it_returns_messages_and_sessions(self, mem, zep):
        self._threads(zep, thread("t-1"))
        zep.thread.get.result = SimpleNamespace(messages=[message(content="hi")])
        body = (await mem.get(self.PATH)).json()
        assert body["message_count"] == 1
        assert body["messages"][0]["content"] == "hi"
        assert body["messages"][0]["thread_id"] == "t-1"
        assert body["session_count"] == 1

    async def test_messages_are_newest_first(self, mem, zep):
        self._threads(zep, thread("t-1"))
        zep.thread.get.result = SimpleNamespace(
            messages=[
                message(uuid="old", created_at="2026-01-01T00:00:00Z"),
                message(uuid="new", created_at="2026-06-01T00:00:00Z"),
            ]
        )
        uuids = [m["uuid"] for m in (await mem.get(self.PATH)).json()["messages"]]
        assert uuids == ["new", "old"]

    async def test_a_bare_list_response_is_accepted(self, mem, zep):
        """Older Zep builds returned a list rather than a MessageListResponse."""
        self._threads(zep, thread("t-1"))
        zep.thread.get.result = [message()]
        assert (await mem.get(self.PATH)).json()["message_count"] == 1

    async def test_an_empty_thread_contributes_a_session_but_no_messages(self, mem, zep):
        self._threads(zep, thread("t-1"))
        zep.thread.get.result = SimpleNamespace(messages=[])
        body = (await mem.get(self.PATH)).json()
        assert body["session_count"] == 1
        assert body["message_count"] == 0

    async def test_a_none_response_is_tolerated(self, mem, zep):
        self._threads(zep, thread("t-1"))
        zep.thread.get.result = None
        assert (await mem.get(self.PATH)).json()["message_count"] == 0

    async def test_another_tenants_threads_are_excluded(self, mem, zep):
        self._threads(zep, thread("t-other", user_id="someone-else"))
        body = (await mem.get(self.PATH)).json()
        assert body["session_count"] == 0
        assert not zep.thread.get.called

    async def test_a_thread_created_at_is_stringified(self, mem, zep):
        self._threads(zep, thread("t-1", created_at="2026-03-01"))
        assert (await mem.get(self.PATH)).json()["sessions"][0]["created_at"] == "2026-03-01"

    async def test_a_thread_without_created_at_reports_none(self, mem, zep):
        self._threads(zep, thread("t-1", created_at=None))
        assert (await mem.get(self.PATH)).json()["sessions"][0]["created_at"] is None

    async def test_a_failing_thread_fetch_does_not_lose_the_session(self, mem, zep, caplog):
        self._threads(zep, thread("t-1"))
        zep.thread.get.error = RuntimeError("thread unavailable")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert body["session_count"] == 1
        assert body["message_count"] == 0
        assert "Failed to get messages from thread" in caplog.text

    async def test_a_failing_listing_returns_an_empty_result(self, mem, zep, caplog):
        zep.thread.list_all.error = RuntimeError("threads down")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert body == {"messages": [], "message_count": 0, "sessions": [], "session_count": 0}

    async def test_a_message_missing_a_role_falls_back_to_role_type(self, mem, zep):
        self._threads(zep, thread("t-1"))
        msg = message()
        del msg.role
        zep.thread.get.result = SimpleNamespace(messages=[msg])
        assert (await mem.get(self.PATH)).json()["messages"][0]["role"] == "user"

    async def test_a_message_with_neither_role_field(self, mem, zep):
        self._threads(zep, thread("t-1"))
        msg = message()
        del msg.role
        del msg.role_type
        zep.thread.get.result = SimpleNamespace(messages=[msg])
        body = (await mem.get(self.PATH)).json()
        assert body["messages"][0]["role"] is None
        assert body["messages"][0]["role_type"] is None

    async def test_a_message_without_a_timestamp(self, mem, zep):
        self._threads(zep, thread("t-1"))
        zep.thread.get.result = SimpleNamespace(messages=[message(created_at=None)])
        assert (await mem.get(self.PATH)).json()["messages"][0]["created_at"] is None

    async def test_the_limit_caps_the_returned_messages(self, mem, zep):
        self._threads(zep, thread("t-1"))
        zep.thread.get.result = SimpleNamespace(messages=[message(uuid=f"m{i}") for i in range(5)])
        body = (await mem.get(self.PATH, params={"limit": 2})).json()
        assert len(body["messages"]) == 2
        assert body["message_count"] == 5, "the count is the true total"

    @pytest.mark.parametrize("limit", [0, 501])
    async def test_an_out_of_range_limit_is_a_422(self, mem, limit):
        assert (await mem.get(self.PATH, params={"limit": limit})).status_code == 422

    async def test_another_users_messages_are_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/messages")).status_code == 403


class TestUpdateEdge:
    PATH = f"/memory/{USER_ID}/edge/e-1"

    async def test_it_deletes_and_recreates_with_the_new_fact(self, mem, zep):
        zep.graph.edge.get.result = edge(fact="old fact", name="OLD")
        r = await mem.patch(self.PATH, json={"fact": "new fact"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["old_edge_uuid"] == "e-1"
        assert body["new_edge_uuid"] == "e-new"
        assert body["fact"] == "new fact"
        assert body["name"] == "OLD", "an omitted field keeps its old value"
        assert zep.graph.edge.delete.last["uuid_"] == "e-1"
        assert zep.graph.add_fact_triple.last["fact"] == "new fact"

    async def test_every_field_can_be_replaced(self, mem, zep):
        zep.graph.edge.get.result = edge()
        body = (
            await mem.patch(
                self.PATH,
                json={
                    "fact": "f",
                    "name": "N",
                    "source_node_uuid": "s2",
                    "target_node_uuid": "t2",
                    "valid_at": "2026-03-01T00:00:00Z",
                    "invalid_at": "2026-04-01T00:00:00Z",
                },
            )
        ).json()
        assert body["source_node_uuid"] == "s2"
        assert body["target_node_uuid"] == "t2"
        triple = zep.graph.add_fact_triple.last
        assert triple["valid_at"] == "2026-03-01T00:00:00Z"
        assert triple["invalid_at"] == "2026-04-01T00:00:00Z"

    async def test_existing_temporal_fields_are_carried_forward(self, mem, zep):
        zep.graph.edge.get.result = edge(valid_at="2026-01-01", invalid_at="2026-02-01")
        await mem.patch(self.PATH, json={"fact": "f"})
        triple = zep.graph.add_fact_triple.last
        assert triple["valid_at"] == "2026-01-01"
        assert triple["invalid_at"] == "2026-02-01"

    async def test_absent_temporal_fields_are_not_sent(self, mem, zep):
        zep.graph.edge.get.result = edge(valid_at=None, invalid_at=None)
        await mem.patch(self.PATH, json={"fact": "f"})
        assert "valid_at" not in zep.graph.add_fact_triple.last
        assert "invalid_at" not in zep.graph.add_fact_triple.last

    async def test_an_edge_with_no_fact_gets_a_default(self, mem, zep):
        zep.graph.edge.get.result = edge(fact=None, name=None)
        body = (await mem.patch(self.PATH, json={})).json()
        assert body["fact"] == "related"
        assert body["name"] == "RELATED_TO"

    @pytest.mark.parametrize("missing", ["source", "target"])
    async def test_an_edge_without_both_endpoints_is_a_400(self, mem, zep, missing):
        zep.graph.edge.get.result = edge(
            source=None if missing == "source" else "n-1",
            target=None if missing == "target" else "n-2",
        )
        r = await mem.patch(self.PATH, json={"fact": "f"})
        assert r.status_code == 400
        assert "source and target" in r.json()["detail"]
        assert not zep.graph.edge.delete.called, "nothing is destroyed on a rejected edit"

    async def test_an_unknown_edge_is_a_404(self, mem, zep):
        zep.graph.edge.get.error = RuntimeError("status 404 not found")
        r = await mem.patch(self.PATH, json={"fact": "f"})
        assert r.status_code == 404

    async def test_another_fetch_failure_is_a_500(self, mem, zep):
        zep.graph.edge.get.error = RuntimeError("zep down")
        assert (await mem.patch(self.PATH, json={"fact": "f"})).status_code == 500

    async def test_a_recreate_failure_is_a_500(self, mem, zep):
        zep.graph.edge.get.result = edge()
        zep.graph.add_fact_triple.error = RuntimeError("triple rejected")
        assert (await mem.patch(self.PATH, json={"fact": "f"})).status_code == 500

    async def test_a_result_exposing_uuid_underscore_is_read(self, mem, zep):
        zep.graph.edge.get.result = edge()
        zep.graph.add_fact_triple.result = SimpleNamespace(uuid_="e-alt")
        assert (await mem.patch(self.PATH, json={"fact": "f"})).json()["new_edge_uuid"] == "e-alt"

    async def test_a_result_with_neither_uuid_field(self, mem, zep):
        zep.graph.edge.get.result = edge()
        zep.graph.add_fact_triple.result = SimpleNamespace()
        assert (await mem.patch(self.PATH, json={"fact": "f"})).json()["new_edge_uuid"] is None

    async def test_another_users_edge_is_a_403(self, mem, zep):
        r = await mem.patch("/memory/someone-else/edge/e-1", json={"fact": "f"})
        assert r.status_code == 403
        assert not zep.graph.edge.delete.called


class TestUpdateNode:
    PATH = f"/memory/{USER_ID}/node/n-1"

    async def test_a_node_with_no_edges_is_recreated_standalone(self, mem, zep):
        zep.graph.node.get.result = node(name="Old", summary="s")
        zep.graph.node.get_edges.result = []
        body = (await mem.patch(self.PATH, json={"name": "New"})).json()
        assert body["success"] is True
        assert body["name"] == "New"
        assert body["recreated_edges"] == 0
        assert body["new_node_uuid"] == "n-new"
        triple = zep.graph.add_fact_triple.last
        assert triple["fact"] == "New exists"
        assert triple["fact_name"] == "EXISTS"

    async def test_an_omitted_name_keeps_the_old_one(self, mem, zep):
        zep.graph.node.get.result = node(name="Old")
        body = (await mem.patch(self.PATH, json={"summary": "new summary"})).json()
        assert body["name"] == "Old"
        assert body["summary"] == "new summary"

    async def test_a_node_without_a_name_gets_a_default(self, mem, zep):
        zep.graph.node.get.result = node(name=None)
        assert (await mem.patch(self.PATH, json={})).json()["name"] == "Unknown"

    async def test_an_outgoing_edge_is_recreated_on_the_source_side(self, mem, zep):
        zep.graph.node.get.result = node(name="Old", summary="s")
        zep.graph.node.get_edges.result = [edge(source="n-1", target="n-2", fact="f")]
        body = (await mem.patch(self.PATH, json={"name": "New"})).json()
        assert body["recreated_edges"] == 1
        triple = zep.graph.add_fact_triple.last
        assert triple["source_node_name"] == "New"
        assert triple["target_node_uuid"] == "n-2"
        assert triple["source_node_summary"] == "s"

    async def test_an_incoming_edge_is_recreated_on_the_target_side(self, mem, zep):
        zep.graph.node.get.result = node(name="Old", summary="s")
        zep.graph.node.get_edges.result = [edge(source="n-0", target="n-1")]
        body = (await mem.patch(self.PATH, json={"name": "New"})).json()
        assert body["recreated_edges"] == 1
        triple = zep.graph.add_fact_triple.last
        assert triple["target_node_name"] == "New"
        assert triple["source_node_uuid"] == "n-0"

    async def test_an_unrelated_edge_is_skipped(self, mem, zep):
        zep.graph.node.get.result = node(name="Old")
        zep.graph.node.get_edges.result = [edge(source="n-7", target="n-8")]
        body = (await mem.patch(self.PATH, json={"name": "New"})).json()
        assert body["recreated_edges"] == 0

    async def test_a_node_with_no_summary_omits_it_from_the_triple(self, mem, zep):
        zep.graph.node.get.result = node(name="Old", summary=None)
        zep.graph.node.get_edges.result = [edge(source="n-1", target="n-2")]
        await mem.patch(self.PATH, json={"name": "New"})
        assert "source_node_summary" not in zep.graph.add_fact_triple.last

    async def test_edge_temporal_fields_are_carried(self, mem, zep):
        zep.graph.node.get.result = node()
        zep.graph.node.get_edges.result = [
            edge(source="n-1", target="n-2", valid_at="2026-01-01", invalid_at="2026-02-01")
        ]
        await mem.patch(self.PATH, json={"name": "New"})
        triple = zep.graph.add_fact_triple.last
        assert triple["valid_at"] == "2026-01-01"
        assert triple["invalid_at"] == "2026-02-01"

    async def test_an_edge_without_a_fact_gets_defaults(self, mem, zep):
        zep.graph.node.get.result = node()
        zep.graph.node.get_edges.result = [edge(source="n-1", target="n-2", fact=None, name=None)]
        await mem.patch(self.PATH, json={"name": "New"})
        triple = zep.graph.add_fact_triple.last
        assert triple["fact"] == "related"
        assert triple["fact_name"] == "RELATED_TO"

    async def test_a_failing_edge_recreate_does_not_abort_the_rest(self, mem, zep, caplog):
        zep.graph.node.get.result = node()
        zep.graph.node.get_edges.result = [edge(source="n-1", target="n-2")]
        zep.graph.add_fact_triple.error = RuntimeError("triple rejected")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.patch(self.PATH, json={"name": "New"})).json()
        assert body["success"] is True
        assert body["recreated_edges"] == 0
        assert "Failed to recreate edge" in caplog.text

    async def test_a_failing_standalone_recreate_is_logged(self, mem, zep, caplog):
        zep.graph.node.get.result = node()
        zep.graph.node.get_edges.result = []
        zep.graph.add_fact_triple.error = RuntimeError("triple rejected")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.patch(self.PATH, json={"name": "New"})).json()
        assert body["new_node_uuid"] is None
        assert "Failed to recreate standalone node" in caplog.text

    async def test_a_failing_edge_lookup_falls_back_to_standalone(self, mem, zep):
        zep.graph.node.get.result = node()
        zep.graph.node.get_edges.error = RuntimeError("edges unavailable")
        body = (await mem.patch(self.PATH, json={"name": "New"})).json()
        assert body["recreated_edges"] == 0
        assert zep.graph.add_fact_triple.last["fact_name"] == "EXISTS"

    async def test_an_unknown_node_is_a_404(self, mem, zep):
        zep.graph.node.get.error = RuntimeError("status 404")
        assert (await mem.patch(self.PATH, json={"name": "x"})).status_code == 404

    async def test_another_fetch_failure_is_a_500(self, mem, zep):
        zep.graph.node.get.error = RuntimeError("zep down")
        assert (await mem.patch(self.PATH, json={"name": "x"})).status_code == 500

    async def test_a_delete_failure_is_a_500(self, mem, zep):
        zep.graph.node.get.result = node()
        zep.graph.node.delete.error = RuntimeError("delete refused")
        assert (await mem.patch(self.PATH, json={"name": "x"})).status_code == 500

    async def test_a_result_without_the_expected_uuid_field(self, mem, zep):
        zep.graph.node.get.result = node()
        zep.graph.node.get_edges.result = [edge(source="n-1", target="n-2")]
        zep.graph.add_fact_triple.result = SimpleNamespace()
        assert (await mem.patch(self.PATH, json={})).json()["new_node_uuid"] is None

    async def test_another_users_node_is_a_403(self, mem, zep):
        r = await mem.patch("/memory/someone-else/node/n-1", json={"name": "x"})
        assert r.status_code == 403
        assert not zep.graph.node.delete.called
