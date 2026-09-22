"""`/memory` — communities, duplicates, and merge.

These are the routes that do real computation rather than reshaping Zep's
output: Louvain community detection over networkx, fuzzy name matching through
thefuzz, and an edge-repointing merge. The libraries are left real — they are
deterministic and fast — so these assert on actual clustering and scoring.
"""

from __future__ import annotations

import pytest

from tests.unit.api.memory.conftest import USER_ID, edge, node

pytestmark = pytest.mark.anyio


class TestCommunities:
    PATH = f"/memory/{USER_ID}/communities"

    async def test_an_empty_graph(self, mem, zep):
        assert (await mem.get(self.PATH)).json() == {"communities": [], "count": 0}

    async def test_two_disconnected_clusters_are_two_communities(self, mem, zep):
        """Two triangles with no edge between them must not be merged."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid=f"a{i}", name=f"A{i}") for i in range(3)
        ] + [node(uuid=f"b{i}", name=f"B{i}") for i in range(3)]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid="e1", source="a0", target="a1"),
            edge(uuid="e2", source="a1", target="a2"),
            edge(uuid="e3", source="a0", target="a2"),
            edge(uuid="e4", source="b0", target="b1"),
            edge(uuid="e5", source="b1", target="b2"),
            edge(uuid="e6", source="b0", target="b2"),
        ]
        body = (await mem.get(self.PATH)).json()
        assert body["count"] == 2
        assert {c["size"] for c in body["communities"]} == {3}

    async def test_communities_are_ordered_largest_first(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid=f"a{i}", name=f"A{i}") for i in range(4)
        ] + [node(uuid="b0", name="B0")]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid="e1", source="a0", target="a1"),
            edge(uuid="e2", source="a1", target="a2"),
            edge(uuid="e3", source="a2", target="a3"),
            edge(uuid="e4", source="a3", target="a0"),
        ]
        sizes = [c["size"] for c in (await mem.get(self.PATH)).json()["communities"]]
        assert sizes == sorted(sizes, reverse=True)

    async def test_a_community_is_labelled_by_its_members(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Ada"),
            node(uuid="n-2", name="Grace"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        label = (await mem.get(self.PATH)).json()["communities"][0]["label"]
        assert "Ada" in label and "Grace" in label

    async def test_a_large_community_label_is_truncated(self, mem, zep):
        """A clique, so Louvain cannot split it and the community really is >5."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid=f"n{i}", name=f"Name{i}") for i in range(8)
        ]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid=f"e{i}-{j}", source=f"n{i}", target=f"n{j}")
            for i in range(8)
            for j in range(i + 1, 8)
        ]
        body = (await mem.get(self.PATH)).json()
        biggest = max(body["communities"], key=lambda c: c["size"])
        assert biggest["size"] > 5
        assert biggest["label"].endswith(f"(+{biggest['size'] - 5} more)")
        assert biggest["label"].count(",") == 4, "five names joined, then the marker"

    async def test_isolated_nodes_are_their_own_communities(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="A"),
            node(uuid="n-2", name="B"),
        ]
        assert (await mem.get(self.PATH)).json()["count"] == 2

    async def test_nodes_without_a_uuid_are_not_added_to_the_graph(self, mem, zep):
        bare = node(name="NoUuid")
        del bare.uuid_
        del bare.uuid
        zep.graph.node.get_by_user_id.result = [bare]
        assert (await mem.get(self.PATH)).json() == {"communities": [], "count": 0}

    async def test_an_edge_to_an_unknown_node_is_ignored(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="A")]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-missing")]
        assert (await mem.get(self.PATH)).json()["count"] == 1

    async def test_the_member_payload_carries_the_node_fields(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Ada", summary="A person", labels=["Person"])
        ]
        member = (await mem.get(self.PATH)).json()["communities"][0]["members"][0]
        assert member == {"uuid": "n-1", "name": "Ada", "summary": "A person", "labels": ["Person"]}

    @pytest.mark.parametrize("resolution", [0.1, 1.0, 5.0])
    async def test_the_resolution_bounds_are_accepted(self, mem, zep, resolution):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="A")]
        r = await mem.get(self.PATH, params={"resolution": resolution})
        assert r.status_code == 200

    @pytest.mark.parametrize("resolution", [0.05, 5.1])
    async def test_an_out_of_range_resolution_is_a_422(self, mem, resolution):
        r = await mem.get(self.PATH, params={"resolution": resolution})
        assert r.status_code == 422

    async def test_a_failure_is_a_500(self, monkeypatch, mem, zep, caplog):
        from src.api.routes.memory import analysis as mem_mod

        async def boom(client, user_id, limit=200):
            raise RuntimeError("fetch exploded")

        monkeypatch.setattr(mem_mod, "_fetch_graph_raw", boom)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.get(self.PATH)).status_code == 500
        assert "Failed to detect communities" in caplog.text

    async def test_another_users_graph_is_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/communities")).status_code == 403


class TestDuplicates:
    PATH = f"/memory/{USER_ID}/duplicates"

    async def test_fewer_than_two_nodes_has_no_duplicates(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="Ada")]
        assert (await mem.get(self.PATH)).json() == {"duplicates": [], "count": 0}

    async def test_near_identical_names_are_flagged(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelonaa"),
        ]
        body = (await mem.get(self.PATH)).json()
        assert body["count"] == 1
        assert body["duplicates"][0]["score"] >= 80

    async def test_unrelated_names_are_not_flagged(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Quetzalcoatl"),
        ]
        assert (await mem.get(self.PATH)).json()["count"] == 0

    async def test_reordered_words_are_caught_by_the_token_ratio(self, mem, zep):
        """`fuzz.ratio` alone misses this; the token_sort_ratio is why both are used."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Ada Lovelace"),
            node(uuid="n-2", name="Lovelace Ada"),
        ]
        assert (await mem.get(self.PATH)).json()["count"] == 1

    async def test_the_more_connected_node_is_kept(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelonaa"),
        ]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid="e1", source="n-2", target="n-9"),
            edge(uuid="e2", source="n-2", target="n-8"),
        ]
        pair = (await mem.get(self.PATH)).json()["duplicates"][0]
        assert pair["keep"]["uuid"] == "n-2"
        assert pair["keep"]["edge_count"] == 2
        assert pair["remove"]["uuid"] == "n-1"

    async def test_a_tie_keeps_the_first(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelonaa"),
        ]
        assert (await mem.get(self.PATH)).json()["duplicates"][0]["keep"]["uuid"] == "n-1"

    async def test_the_threshold_is_honoured(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelonaa"),
        ]
        assert (await mem.get(self.PATH, params={"threshold": 100})).json()["count"] == 0
        assert (await mem.get(self.PATH, params={"threshold": 50})).json()["count"] == 1

    async def test_nameless_nodes_are_skipped(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name=""),
            node(uuid="n-2", name=""),
        ]
        assert (await mem.get(self.PATH)).json()["count"] == 0

    async def test_results_are_ordered_by_score(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
            node(uuid="n-3", name="Barcelonaa"),
        ]
        scores = [d["score"] for d in (await mem.get(self.PATH)).json()["duplicates"]]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.parametrize("threshold", [49, 101])
    async def test_an_out_of_range_threshold_is_a_422(self, mem, threshold):
        r = await mem.get(self.PATH, params={"threshold": threshold})
        assert r.status_code == 422

    async def test_a_failure_is_a_500(self, monkeypatch, mem, zep, caplog):
        from src.api.routes.memory import analysis as mem_mod

        async def boom(client, user_id, limit=200):
            raise RuntimeError("fetch exploded")

        monkeypatch.setattr(mem_mod, "_fetch_graph_raw", boom)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.get(self.PATH)).status_code == 500
        assert "Failed to detect duplicates" in caplog.text

    async def test_another_users_graph_is_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/duplicates")).status_code == 403


class TestMerge:
    PATH = f"/memory/{USER_ID}/merge"

    def _body(self, keep="n-keep", remove="n-remove"):
        return {"keep_uuid": keep, "remove_uuid": remove}

    async def test_edges_are_repointed_and_the_duplicate_deleted(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-keep", name="Barcelona")
        zep.graph.node.get_edges.result = [
            edge(uuid="e1", source="n-remove", target="n-other", fact="is in", name="LOCATED_IN")
        ]
        body = (await mem.post(self.PATH, json=self._body())).json()
        assert body["success"] is True
        assert body["keep_name"] == "Barcelona"
        assert body["recreated_edges"] == 1
        triple = zep.graph.add_fact_triple.last
        assert triple["source_node_uuid"] == "n-keep"
        assert triple["target_node_uuid"] == "n-other"
        assert zep.graph.node.delete.last["uuid_"] == "n-remove"

    async def test_an_incoming_edge_is_repointed_on_the_target_side(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.result = [edge(source="n-other", target="n-remove")]
        await mem.post(self.PATH, json=self._body())
        triple = zep.graph.add_fact_triple.last
        assert triple["source_node_uuid"] == "n-other"
        assert triple["target_node_uuid"] == "n-keep"

    async def test_an_edge_between_the_two_merging_nodes_is_dropped(self, mem, zep):
        """Repointing it would create a self-loop on the kept node."""
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.result = [edge(source="n-remove", target="n-keep")]
        body = (await mem.post(self.PATH, json=self._body())).json()
        assert body["recreated_edges"] == 0
        assert not zep.graph.add_fact_triple.called

    async def test_an_unrelated_edge_is_skipped(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.result = [edge(source="n-7", target="n-8")]
        assert (await mem.post(self.PATH, json=self._body())).json()["recreated_edges"] == 0

    async def test_temporal_fields_survive_the_merge(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.result = [
            edge(
                source="n-remove", target="n-other", valid_at="2026-01-01", invalid_at="2026-02-01"
            )
        ]
        await mem.post(self.PATH, json=self._body())
        triple = zep.graph.add_fact_triple.last
        assert triple["valid_at"] == "2026-01-01"
        assert triple["invalid_at"] == "2026-02-01"

    async def test_an_edge_without_a_fact_gets_defaults(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.result = [
            edge(source="n-remove", target="n-other", fact=None, name=None)
        ]
        await mem.post(self.PATH, json=self._body())
        assert zep.graph.add_fact_triple.last["fact"] == "related"
        assert zep.graph.add_fact_triple.last["fact_name"] == "RELATED_TO"

    async def test_an_unknown_keep_node_is_a_404(self, mem, zep):
        zep.graph.node.get.error = RuntimeError("not found")
        r = await mem.post(self.PATH, json=self._body())
        assert r.status_code == 404
        assert not zep.graph.node.delete.called

    async def test_a_failing_edge_lookup_still_deletes_the_duplicate(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.error = RuntimeError("edges unavailable")
        body = (await mem.post(self.PATH, json=self._body())).json()
        assert body["recreated_edges"] == 0
        assert zep.graph.node.delete.called

    async def test_a_failing_recreate_does_not_abort_the_merge(self, mem, zep, caplog):
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.result = [edge(source="n-remove", target="n-other")]
        zep.graph.add_fact_triple.error = RuntimeError("triple rejected")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.post(self.PATH, json=self._body())).json()
        assert body["success"] is True
        assert body["recreated_edges"] == 0
        assert "Failed to recreate edge during merge" in caplog.text

    async def test_a_failing_delete_still_reports_success(self, mem, zep, caplog):
        """The edges are already repointed; the orphaned node is the lesser problem."""
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.delete.error = RuntimeError("delete refused")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.post(self.PATH, json=self._body())).json()
        assert body["success"] is True
        assert "Failed to delete merged node" in caplog.text

    async def test_a_node_without_a_name(self, mem, zep):
        bare = node(uuid="n-keep")
        del bare.name
        zep.graph.node.get.result = bare
        assert (await mem.post(self.PATH, json=self._body())).json()["keep_name"] == "Unknown"

    async def test_no_edges_at_all(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-keep")
        zep.graph.node.get_edges.result = None
        assert (await mem.post(self.PATH, json=self._body())).json()["recreated_edges"] == 0

    async def test_another_users_graph_is_a_403(self, mem, zep):
        r = await mem.post("/memory/someone-else/merge", json=self._body())
        assert r.status_code == 403
        assert not zep.graph.node.delete.called
