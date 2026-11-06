"""`/memory` — messages, and the edge/node edit routes.

Zep updates edges and nodes in place (`graph.edge.update`, `graph.node.update`),
so an edit keeps the record's uuid, episodes and every fact it takes part in.
The old routes deleted and recreated, rebuilding a node's edges from
`graph.node.get_edges`, which under-reports -- renaming an entity lost its facts.
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
    """A fact is edited where it stands; only a change of endpoints recreates it."""

    PATH = f"/memory/{USER_ID}/edge/e-1"

    async def test_the_fact_is_updated_in_place(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1", fact="old", source="n-1", target="n-2")
        zep.graph.edge.update.result = edge(uuid="e-1", fact="new", source="n-1", target="n-2")
        body = (await mem.patch(self.PATH, json={"fact": "new"})).json()
        assert zep.graph.edge.update.last == {"uuid_": "e-1", "fact": "new"}
        assert not zep.graph.edge.delete.called
        assert not zep.graph.add_fact_triple.called
        assert body["old_edge_uuid"] == body["new_edge_uuid"] == "e-1"
        assert body["fact"] == "new"

    async def test_only_the_fields_sent_are_changed(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1")
        await mem.patch(
            self.PATH,
            json={"name": "WORKS_AT", "valid_at": "2026-01-01", "invalid_at": "2026-02-01"},
        )
        assert zep.graph.edge.update.last == {
            "uuid_": "e-1",
            "name": "WORKS_AT",
            "valid_at": "2026-01-01",
            "invalid_at": "2026-02-01",
        }

    async def test_an_update_result_without_fields_echoes_the_change(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1", fact="old", name="R")
        zep.graph.edge.update.result = None
        body = (await mem.patch(self.PATH, json={"fact": "new"})).json()
        assert body["fact"] == "new"
        assert body["name"] == "R"

    async def test_unchanged_endpoints_sent_explicitly_stay_in_place(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1", source="n-1", target="n-2")
        await mem.patch(self.PATH, json={"source_node_uuid": "n-1", "fact": "x"})
        assert zep.graph.edge.update.called
        assert not zep.graph.edge.delete.called

    async def test_new_endpoints_write_the_new_fact_before_removing_the_old(self, mem, zep):
        order = []
        zep.graph.edge.get.result = edge(
            uuid="e-1",
            fact="old",
            name="KNOWS",
            source="n-1",
            target="n-2",
            valid_at="2026-01-01",
            attributes={"since": "2020"},
        )

        async def add_triple(**kwargs):
            order.append(("add", kwargs))
            return SimpleNamespace(edge_uuid="e-new")

        async def delete(**kwargs):
            order.append(("delete", kwargs))

        zep.graph.add_fact_triple = add_triple
        zep.graph.edge.delete = delete
        body = (await mem.patch(self.PATH, json={"target_node_uuid": "n-3"})).json()
        assert [step for step, _ in order] == ["add", "delete"]
        triple = order[0][1]
        assert triple["source_node_uuid"] == "n-1"
        assert triple["target_node_uuid"] == "n-3"
        assert triple["fact"] == "old"
        assert triple["fact_name"] == "KNOWS"
        assert triple["valid_at"] == "2026-01-01"
        assert triple["edge_attributes"] == {"since": "2020"}
        assert body["new_edge_uuid"] == "e-new"

    async def test_a_failed_move_keeps_the_old_fact(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1", source="n-1", target="n-2")
        zep.graph.add_fact_triple.error = RuntimeError("triple rejected")
        r = await mem.patch(self.PATH, json={"target_node_uuid": "n-3"})
        assert r.status_code == 500
        assert not zep.graph.edge.delete.called

    async def test_a_move_without_a_fact_gets_defaults(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1", fact=None, name=None)
        zep.graph.add_fact_triple.result = SimpleNamespace(uuid_="e-new")
        body = (await mem.patch(self.PATH, json={"source_node_uuid": "n-9"})).json()
        assert zep.graph.add_fact_triple.last["fact"] == "related"
        assert zep.graph.add_fact_triple.last["fact_name"] == "RELATED_TO"
        assert "valid_at" not in zep.graph.add_fact_triple.last
        assert "edge_attributes" not in zep.graph.add_fact_triple.last
        assert body["new_edge_uuid"] == "e-new"

    async def test_a_move_with_a_new_fact_and_window(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1")
        zep.graph.add_fact_triple.result = SimpleNamespace()
        body = (
            await mem.patch(
                self.PATH,
                json={"target_node_uuid": "n-3", "fact": "moved", "invalid_at": "2026-05-01"},
            )
        ).json()
        assert zep.graph.add_fact_triple.last["fact"] == "moved"
        assert zep.graph.add_fact_triple.last["invalid_at"] == "2026-05-01"
        assert body["new_edge_uuid"] is None

    async def test_an_edge_missing_an_endpoint_is_a_400(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1", source=None, target="n-2")
        assert (await mem.patch(self.PATH, json={"fact": "x"})).status_code == 400

    async def test_an_unknown_edge_is_a_404(self, mem, zep):
        zep.graph.edge.get.error = RuntimeError("404 not found")
        assert (await mem.patch(self.PATH, json={"fact": "x"})).status_code == 404

    async def test_another_fetch_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.edge.get.error = RuntimeError("zep down")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.patch(self.PATH, json={"fact": "x"})).status_code == 500
        assert "Failed to update edge" in caplog.text

    async def test_an_update_failure_is_a_500(self, mem, zep):
        zep.graph.edge.get.result = edge(uuid="e-1")
        zep.graph.edge.update.error = RuntimeError("rejected")
        assert (await mem.patch(self.PATH, json={"fact": "x"})).status_code == 500

    async def test_another_users_edge_is_a_403(self, mem):
        r = await mem.patch("/memory/someone-else/edge/e-1", json={"fact": "x"})
        assert r.status_code == 403


class TestUpdateNode:
    """Renaming an entity keeps its uuid and every fact it takes part in."""

    PATH = f"/memory/{USER_ID}/node/n-1"

    async def test_it_updates_in_place(self, mem, zep):
        zep.graph.node.update.result = node(uuid="n-1", name="New", summary="s2", labels=["Person"])
        body = (await mem.patch(self.PATH, json={"name": "New", "summary": "s2"})).json()
        assert zep.graph.node.update.last == {"uuid_": "n-1", "name": "New", "summary": "s2"}
        assert body["old_node_uuid"] == body["new_node_uuid"] == "n-1"
        assert body["name"] == "New"
        assert body["labels"] == ["Person"]
        assert body["recreated_edges"] == 0

    async def test_no_fact_is_touched(self, mem, zep):
        """The old route deleted the node, cascading to all its edges."""
        await mem.patch(self.PATH, json={"name": "New"})
        assert not zep.graph.node.delete.called
        assert not zep.graph.add_fact_triple.called
        assert not zep.graph.node.get_edges.called

    async def test_labels_are_applied(self, mem, zep):
        """They were accepted and silently ignored."""
        await mem.patch(self.PATH, json={"labels": ["Person", "Friend"]})
        assert zep.graph.node.update.last == {"uuid_": "n-1", "labels": ["Person", "Friend"]}

    async def test_an_update_result_without_fields_echoes_the_change(self, mem, zep):
        zep.graph.node.update.result = None
        body = (await mem.patch(self.PATH, json={"name": "New", "labels": ["Topic"]})).json()
        assert body["name"] == "New"
        assert body["summary"] is None
        assert body["labels"] == ["Topic"]

    async def test_an_empty_patch_is_a_400(self, mem, zep):
        assert (await mem.patch(self.PATH, json={})).status_code == 400
        assert not zep.graph.node.update.called

    async def test_an_unknown_node_is_a_404(self, mem, zep):
        zep.graph.node.update.error = RuntimeError("404 not found")
        assert (await mem.patch(self.PATH, json={"name": "x"})).status_code == 404

    async def test_another_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.node.update.error = RuntimeError("zep down")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.patch(self.PATH, json={"name": "x"})).status_code == 500
        assert "Failed to update node" in caplog.text

    async def test_another_users_node_is_a_403(self, mem):
        r = await mem.patch("/memory/someone-else/node/n-1", json={"name": "x"})
        assert r.status_code == 403
