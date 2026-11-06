"""A uuid in the path or body must be in the caller's own graph.

`verify_user_access` only proves the caller owns `/memory/{user_id}`. Zep deletes
and rewrites any uuid in the project, so before these checks a caller could
delete, edit, merge or connect another tenant's nodes and facts by naming their
uuids. Each route now asks Zep, through the caller's own listings, whether the
uuid is there, and answers 404 -- not 403 -- when it is not, so the check does
not reveal that the uuid exists elsewhere.
"""

from __future__ import annotations

import pytest

from src.services.memory.ownership import edge_in_graph, nodes_in_graph
from tests.unit.api.memory.conftest import USER_ID, FakeZep, edge, node

pytestmark = pytest.mark.anyio

BASE = f"/memory/{USER_ID}"


class TestEdgeInGraph:
    async def test_an_own_edge_is_found(self):
        zep = FakeZep()
        assert await edge_in_graph(zep, USER_ID, "e-1")
        probe = zep.ownership.probes[0]
        assert probe["user_id"] == USER_ID
        assert probe["filters"].edge_uuids == ["e-1"]

    async def test_a_foreign_edge_is_not(self):
        zep = FakeZep()
        zep.ownership.foreign.add("e-1")
        assert not await edge_in_graph(zep, USER_ID, "e-1")

    async def test_a_filter_that_was_ignored_fails_closed(self):
        """If Zep answered with some other edge, the uuid is not taken as found."""
        zep = FakeZep()

        async def unfiltered(**kwargs):
            return [edge(uuid="something-else")]

        zep.graph.edge.get_by_user_id = unfiltered
        assert not await edge_in_graph(zep, USER_ID, "e-1")


class TestNodesInGraph:
    async def test_connected_nodes_are_found_in_one_probe(self):
        zep = FakeZep()
        assert await nodes_in_graph(zep, USER_ID, ["n-1", "n-2"]) == {"n-1", "n-2"}
        assert len(zep.ownership.probes) == 1
        assert zep.ownership.probes[0]["filters"].connected_node_uuids == ["n-1", "n-2"]
        assert not zep.graph.node.get_by_user_id.called

    async def test_nothing_asked_is_nothing_found(self):
        zep = FakeZep()
        assert await nodes_in_graph(zep, USER_ID, ["", None]) == set()
        assert not zep.ownership.probes

    async def test_a_node_without_edges_is_found_in_the_node_listing(self):
        zep = FakeZep()
        zep.ownership.edgeless.add("n-orphan")
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-other-orphan", degree=0),
            node(uuid="n-orphan", degree=0),
        ]
        assert await nodes_in_graph(zep, USER_ID, ["n-orphan"]) == {"n-orphan"}
        listing = zep.graph.node.get_by_user_id.last
        assert listing["order_by"] == "degree"
        assert listing["direction"] == "asc"

    async def test_the_node_walk_stops_at_the_first_connected_node(self):
        """Least-connected first: past a node with edges there are no orphans left."""
        zep = FakeZep()
        zep.ownership.foreign.add("n-theirs")
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-connected", degree=3),
            node(uuid="n-theirs", degree=0),
        ]
        assert await nodes_in_graph(zep, USER_ID, ["n-theirs"]) == set()

    async def test_an_unknown_degree_does_not_stop_the_walk(self):
        zep = FakeZep()
        zep.ownership.edgeless.add("n-orphan")
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-a"),
            node(uuid="n-orphan"),
        ]
        assert await nodes_in_graph(zep, USER_ID, ["n-orphan"]) == {"n-orphan"}

    async def test_a_foreign_node_is_not_found_anywhere(self):
        zep = FakeZep()
        zep.ownership.foreign.add("n-theirs")
        zep.graph.node.get_by_user_id.result = [node(uuid="n-mine", degree=0)]
        assert await nodes_in_graph(zep, USER_ID, ["n-mine", "n-theirs"]) == {"n-mine"}


class TestDeleteEdge:
    async def test_a_foreign_edge_is_404_and_untouched(self, mem, zep):
        zep.ownership.foreign.add("e-theirs")
        response = await mem.delete(f"{BASE}/edge/e-theirs")
        assert response.status_code == 404
        assert not zep.graph.edge.delete.called

    async def test_an_own_edge_is_deleted(self, mem, zep):
        assert (await mem.delete(f"{BASE}/edge/e-1")).status_code == 200
        assert zep.graph.edge.delete.last == {"uuid_": "e-1"}


class TestDeleteNode:
    async def test_a_foreign_node_is_404_and_untouched(self, mem, zep):
        zep.ownership.foreign.add("n-theirs")
        response = await mem.delete(f"{BASE}/node/n-theirs")
        assert response.status_code == 404
        assert not zep.graph.node.delete.called

    async def test_a_probe_failure_is_not_a_grant(self, mem, zep):
        zep.ownership.error = RuntimeError("zep down")
        with pytest.raises(RuntimeError):
            await mem.delete(f"{BASE}/node/n-1")
        assert not zep.graph.node.delete.called


class TestUpdateEdge:
    async def test_a_foreign_edge_is_404(self, mem, zep):
        zep.ownership.foreign.add("e-theirs")
        response = await mem.patch(f"{BASE}/edge/e-theirs", json={"fact": "x"})
        assert response.status_code == 404
        assert not zep.graph.edge.delete.called

    async def test_a_foreign_new_endpoint_is_404(self, mem, zep):
        """Re-pointing an own fact at another tenant's node would write into their graph."""
        zep.ownership.foreign.add("n-theirs")
        response = await mem.patch(f"{BASE}/edge/e-1", json={"target_node_uuid": "n-theirs"})
        assert response.status_code == 404
        assert not zep.graph.edge.delete.called


class TestUpdateNode:
    async def test_a_foreign_node_is_404(self, mem, zep):
        zep.ownership.foreign.add("n-theirs")
        response = await mem.patch(f"{BASE}/node/n-theirs", json={"name": "x"})
        assert response.status_code == 404
        assert not zep.graph.node.delete.called


class TestMerge:
    async def test_a_foreign_node_on_either_side_is_404(self, mem, zep):
        zep.ownership.foreign.add("n-theirs")
        for body in (
            {"keep_uuid": "n-1", "remove_uuid": "n-theirs"},
            {"keep_uuid": "n-theirs", "remove_uuid": "n-1"},
        ):
            assert (await mem.post(f"{BASE}/merge", json=body)).status_code == 404
        assert not zep.graph.node.delete.called
        assert not zep.graph.add_fact_triple.called


class TestReorganizeApply:
    async def test_one_foreign_uuid_stops_the_whole_batch(self, mem, zep):
        zep.ownership.foreign.add("n-theirs")
        response = await mem.post(
            f"{BASE}/reorganize/apply",
            json={
                "orphan_uuids": ["n-mine", "n-theirs"],
                "merge_pairs": [{"keep_uuid": "n-a", "remove_uuid": "n-b"}],
            },
        )
        assert response.status_code == 404
        assert not zep.graph.node.delete.called

    async def test_merge_pairs_are_checked_too(self, mem, zep):
        zep.ownership.foreign.add("n-theirs")
        response = await mem.post(
            f"{BASE}/reorganize/apply",
            json={"merge_pairs": [{"keep_uuid": "n-a", "remove_uuid": "n-theirs"}]},
        )
        assert response.status_code == 404
        assert not zep.graph.node.delete.called

    async def test_all_uuids_are_checked_in_one_probe_first(self, mem, zep):
        await mem.post(
            f"{BASE}/reorganize/apply",
            json={
                "orphan_uuids": ["n-1", "n-2"],
                "merge_pairs": [{"keep_uuid": "n-3", "remove_uuid": "n-4"}],
            },
        )
        assert zep.ownership.probes[0]["filters"].connected_node_uuids == [
            "n-1",
            "n-2",
            "n-3",
            "n-4",
        ]


class TestConnect:
    async def test_a_foreign_node_is_404(self, mem, zep):
        zep.ownership.foreign.add("n-theirs")
        response = await mem.post(
            f"{BASE}/connect",
            json={"source_node_uuid": "n-1", "target_node_uuid": "n-theirs", "relation": "KNOWS"},
        )
        assert response.status_code == 404
        assert not zep.graph.add_fact_triple.called
