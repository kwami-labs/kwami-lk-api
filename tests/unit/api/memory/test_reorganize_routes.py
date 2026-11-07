"""`/memory` — reorganize preview/apply, one-click reorganize, and connect.

Reorganization is destructive, which is why it comes in three shapes: a preview
that writes nothing, an apply that acts only on what the caller selected, and a
one-click route that decides for itself. The rules that protect the user are the
same in all three — the user node is never an orphan, and an edge between the two
merging nodes is dropped rather than turned into a self-loop.
"""

from __future__ import annotations

import pytest

from tests.unit.api.memory.conftest import USER_ID, edge, node

pytestmark = pytest.mark.anyio


class TestReorganizePreview:
    PATH = f"/memory/{USER_ID}/reorganize/preview"

    async def test_an_empty_graph(self, mem, zep):
        assert (await mem.post(self.PATH)).json() == {
            "orphans": [],
            "duplicates": [],
            "communities_estimate": 0,
        }

    async def test_it_writes_nothing(self, mem, zep):
        """A preview that mutates would defeat the point of having one."""
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="Orphan")]
        await mem.post(self.PATH)
        assert not zep.graph.node.delete.called
        assert not zep.graph.add_fact_triple.called

    async def test_an_unconnected_node_is_an_orphan(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Orphan", summary="s", labels=["Preference"])
        ]
        body = (await mem.post(self.PATH)).json()
        assert body["orphans"] == [
            {"uuid": "n-1", "name": "Orphan", "summary": "s", "labels": ["Preference"]}
        ]

    async def test_a_connected_node_is_not_an_orphan(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="A"),
            node(uuid="n-2", name="B"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["orphans"] == []

    @pytest.mark.parametrize("name", ["kwami_abc", "user", "USER", "KWAMI_xyz"])
    async def test_the_user_node_is_never_an_orphan(self, mem, zep, name):
        """Deleting it would erase the anchor the whole graph hangs off."""
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name=name)]
        assert (await mem.post(self.PATH)).json()["orphans"] == []

    async def test_a_node_without_a_uuid_is_not_an_orphan(self, mem, zep):
        bare = node(name="NoUuid")
        del bare.uuid_
        del bare.uuid
        zep.graph.node.get_by_user_id.result = [bare]
        assert (await mem.post(self.PATH)).json()["orphans"] == []

    async def test_duplicates_are_detected(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        body = (await mem.post(self.PATH)).json()
        assert len(body["duplicates"]) == 1
        assert body["duplicates"][0]["score"] == 100

    async def test_the_threshold_is_stricter_here_by_default(self, mem, zep):
        """95, not the 80 that `/duplicates` uses -- this one leads to deletion.

        "Barcelonaaa" scores 90: over /duplicates' default, under this one.
        """
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelonaaa"),
        ]
        assert (await mem.post(self.PATH)).json()["duplicates"] == []
        r = await mem.post(self.PATH, params={"duplicate_threshold": 80})
        assert len(r.json()["duplicates"]) == 1

    async def test_the_more_connected_node_is_kept(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-2", target="n-9")]
        pair = (await mem.post(self.PATH)).json()["duplicates"][0]
        assert pair["keep"]["uuid"] == "n-2"
        assert pair["remove"]["uuid"] == "n-1"

    async def test_nameless_nodes_are_skipped_on_both_sides(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name=""),
            node(uuid="n-2", name="Barcelona"),
            node(uuid="n-3", name=""),
        ]
        assert (await mem.post(self.PATH)).json()["duplicates"] == []

    async def test_the_community_estimate(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="A"),
            node(uuid="n-2", name="B"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["communities_estimate"] == 1

    async def test_an_edge_to_an_unknown_node_is_ignored_in_the_estimate(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="A")]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-missing")]
        assert (await mem.post(self.PATH)).json()["communities_estimate"] == 1

    async def test_duplicates_are_ordered_by_score(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
            node(uuid="n-3", name="Barcelonaa"),
        ]
        scores = [
            d["score"]
            for d in (await mem.post(self.PATH, params={"duplicate_threshold": 80})).json()[
                "duplicates"
            ]
        ]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.parametrize("threshold", [79, 101])
    async def test_an_out_of_range_threshold_is_a_422(self, mem, threshold):
        r = await mem.post(self.PATH, params={"duplicate_threshold": threshold})
        assert r.status_code == 422

    async def test_a_failure_is_a_500(self, monkeypatch, mem, zep, caplog):
        from src.api.routes.memory import analysis as mem_mod

        async def boom(client, user_id, limit=200):
            raise RuntimeError("fetch exploded")

        monkeypatch.setattr(mem_mod, "_fetch_graph_raw", boom)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.post(self.PATH)).status_code == 500
        assert "Failed reorganize preview" in caplog.text

    async def test_another_users_graph_is_a_403(self, mem):
        assert (await mem.post("/memory/someone-else/reorganize/preview")).status_code == 403


def _orphans(zep, *uuids):
    """Nodes that really have no edges: Zep's re-check finds none, and the
    ownership check finds them in the node listing."""
    zep.ownership.edgeless.update(uuids)
    zep.graph.node.get_by_user_id.result = [
        node(uuid=uuid, name=f"Orphan {uuid}", degree=0) for uuid in uuids
    ]


PAIR = {"merge_pairs": [{"keep_uuid": "n-keep", "remove_uuid": "n-remove"}]}


class TestReorganizeApply:
    PATH = f"/memory/{USER_ID}/reorganize/apply"

    async def test_nothing_selected_does_nothing(self, mem, zep):
        body = (await mem.post(self.PATH, json={})).json()
        assert body["report"] == {
            "orphans_removed": 0,
            "orphans_kept": 0,
            "merges_performed": 0,
            "errors": [],
        }
        assert not zep.graph.node.delete.called

    async def test_selected_orphans_are_deleted(self, mem, zep):
        _orphans(zep, "n-1", "n-2")
        body = (await mem.post(self.PATH, json={"orphan_uuids": ["n-1", "n-2"]})).json()
        assert body["report"]["orphans_removed"] == 2
        assert len(zep.graph.node.delete.calls) == 2

    async def test_an_orphan_that_gained_a_fact_is_kept(self, mem, zep):
        """The preview can be minutes old; a fact learned since must not be deleted."""
        body = (await mem.post(self.PATH, json={"orphan_uuids": ["n-1"]})).json()
        assert body["report"]["orphans_removed"] == 0
        assert body["report"]["orphans_kept"] == 1
        assert not zep.graph.node.delete.called

    async def test_a_failing_orphan_delete_is_collected(self, mem, zep):
        _orphans(zep, "n-1")
        zep.graph.node.delete.error = RuntimeError("delete refused")
        body = (await mem.post(self.PATH, json={"orphan_uuids": ["n-1"]})).json()
        assert body["report"]["orphans_removed"] == 0
        assert any("Failed to remove orphan n-1" in e for e in body["report"]["errors"])

    async def test_a_selected_merge_repoints_and_deletes(self, mem, zep):
        zep.ownership.edges["n-remove"] = [edge(source="n-remove", target="n-other")]
        body = (await mem.post(self.PATH, json=PAIR)).json()
        assert body["report"]["merges_performed"] == 1
        triple = zep.graph.add_fact_triple.last
        assert triple["source_node_uuid"] == "n-keep"
        assert triple["target_node_uuid"] == "n-other"
        assert zep.graph.node.delete.last["uuid_"] == "n-remove"

    async def test_an_incoming_edge_is_repointed_on_the_target_side(self, mem, zep):
        zep.ownership.edges["n-remove"] = [edge(source="n-other", target="n-remove")]
        await mem.post(self.PATH, json=PAIR)
        triple = zep.graph.add_fact_triple.last
        assert triple["source_node_uuid"] == "n-other"
        assert triple["target_node_uuid"] == "n-keep"

    async def test_an_edge_between_the_pair_is_dropped(self, mem, zep):
        zep.ownership.edges["n-remove"] = [edge(source="n-remove", target="n-keep")]
        await mem.post(self.PATH, json=PAIR)
        assert not zep.graph.add_fact_triple.called

    async def test_a_failing_recreate_keeps_the_duplicate(self, mem, zep):
        """Its unmoved fact would go with it; the pair is reported, not merged."""
        zep.ownership.edges["n-remove"] = [edge(source="n-remove", target="n-other")]
        zep.graph.add_fact_triple.error = RuntimeError("triple rejected")
        body = (await mem.post(self.PATH, json=PAIR)).json()
        assert any("Edge recreate failed" in e for e in body["report"]["errors"])
        assert body["report"]["merges_performed"] == 0
        assert not zep.graph.node.delete.called

    async def test_a_failing_merge_delete_is_collected(self, mem, zep):
        zep.graph.node.delete.error = RuntimeError("delete refused")
        body = (await mem.post(self.PATH, json=PAIR)).json()
        assert body["report"]["merges_performed"] == 0
        assert any("Delete failed for n-remove" in e for e in body["report"]["errors"])

    async def test_another_users_graph_is_a_403(self, mem, zep):
        r = await mem.post("/memory/someone-else/reorganize/apply", json={"orphan_uuids": ["n-1"]})
        assert r.status_code == 403
        assert not zep.graph.node.delete.called


class TestReorganizeGraph:
    PATH = f"/memory/{USER_ID}/reorganize"

    async def test_an_empty_graph_does_nothing(self, mem, zep):
        body = (await mem.post(self.PATH)).json()
        assert body["report"] == {
            "orphans_removed": 0,
            "orphans_kept": 0,
            "merges_performed": 0,
            "communities_found": 0,
            "errors": [],
        }

    async def test_orphans_are_removed(self, mem, zep):
        _orphans(zep, "n-1")
        body = (await mem.post(self.PATH)).json()
        assert body["report"]["orphans_removed"] == 1
        assert zep.graph.node.delete.last["uuid_"] == "n-1"

    async def test_a_node_whose_edges_are_past_the_first_page_is_not_an_orphan(self, mem, zep):
        """The bug this pass had: it read 50 edges, called the rest orphans, and deleted them."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid=f"n-{i:03d}", name=f"Thing {i}") for i in range(60)
        ]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid=f"e-{i:03d}", source="n-user", target=f"n-{i:03d}") for i in range(60)
        ]
        body = (await mem.post(self.PATH)).json()
        assert body["report"]["orphans_removed"] == 0
        assert not zep.graph.node.delete.called

    async def test_a_node_zep_counts_edges_for_is_not_an_orphan(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="Busy", degree=4)]
        assert (await mem.post(self.PATH)).json()["report"]["orphans_removed"] == 0

    async def test_an_orphan_is_rechecked_before_it_is_deleted(self, mem, zep):
        """The listing says no edges; Zep's own answer for that node wins."""
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="Lonely")]
        body = (await mem.post(self.PATH)).json()
        assert body["report"]["orphans_kept"] == 1
        assert not zep.graph.node.delete.called

    @pytest.mark.parametrize("name", ["kwami_abc", "user"])
    async def test_the_user_node_is_never_removed(self, mem, zep, name):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name=name)]
        body = (await mem.post(self.PATH)).json()
        assert body["report"]["orphans_removed"] == 0
        assert not zep.graph.node.delete.called

    async def test_a_node_labelled_user_is_never_removed(self, mem, zep):
        _orphans(zep, "n-1")
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="Daniel", labels=["User"])]
        assert (await mem.post(self.PATH)).json()["report"]["orphans_removed"] == 0

    async def test_a_node_without_a_uuid_is_skipped(self, mem, zep):
        bare = node(name="NoUuid")
        del bare.uuid_
        del bare.uuid
        zep.graph.node.get_by_user_id.result = [bare]
        assert (await mem.post(self.PATH)).json()["report"]["orphans_removed"] == 0

    async def test_a_failing_orphan_delete_is_collected(self, mem, zep):
        _orphans(zep, "n-1")
        zep.graph.node.delete.error = RuntimeError("delete refused")
        body = (await mem.post(self.PATH)).json()
        assert any("Failed to remove orphan n-1" in e for e in body["report"]["errors"])

    async def test_high_confidence_duplicates_are_auto_merged(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2", name="SAME_AS")]
        body = (await mem.post(self.PATH)).json()
        assert body["report"]["merges_performed"] == 1

    async def test_names_that_differ_only_in_accents_are_merged(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="José Pérez"),
            node(uuid="n-2", name="jose  perez"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["report"]["merges_performed"] == 1

    async def test_same_name_different_type_is_not_auto_merged(self, mem, zep):
        """Apple the company and Apple the fruit."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Apple", labels=["Organization"]),
            node(uuid="n-2", name="Apple", labels=["Object"]),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["report"]["merges_performed"] == 0

    async def test_a_low_similarity_pair_is_left_alone(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Quetzalcoatl"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["report"]["merges_performed"] == 0

    async def test_the_threshold_is_configurable(self, mem, zep):
        """ "Barcelonaaa" scores 90 -- under the default 95, over an explicit 80."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelonaaa"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["report"]["merges_performed"] == 0
        r = await mem.post(self.PATH, params={"auto_merge_threshold": 80})
        assert r.json()["report"]["merges_performed"] == 1

    async def test_a_merged_node_is_not_merged_again(self, mem, zep):
        """Three identical names collapse one pair per pass, never a node twice."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
            node(uuid="n-3", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid="e1", source="n-1", target="n-2"),
            edge(uuid="e2", source="n-2", target="n-3"),
        ]
        body = (await mem.post(self.PATH)).json()
        assert body["report"]["merges_performed"] == 1
        removed = [call["uuid_"] for call in zep.graph.node.delete.calls]
        assert len(removed) == len(set(removed)) == 1

    async def test_nameless_nodes_are_skipped(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name=""),
            node(uuid="n-2", name=""),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["report"]["merges_performed"] == 0

    async def test_an_edge_between_the_merging_pair_is_dropped(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        zep.ownership.edges["n-2"] = [edge(source="n-2", target="n-1")]
        await mem.post(self.PATH)
        assert not zep.graph.add_fact_triple.called

    async def test_a_failing_recreate_keeps_the_duplicate(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        zep.ownership.edges["n-2"] = [edge(source="n-2", target="n-other")]
        zep.graph.add_fact_triple.error = RuntimeError("triple rejected")
        body = (await mem.post(self.PATH)).json()
        assert any("Edge recreate failed" in e for e in body["report"]["errors"])
        assert body["report"]["merges_performed"] == 0
        assert not zep.graph.node.delete.called

    async def test_a_failing_merge_delete_is_collected(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        zep.graph.node.delete.error = RuntimeError("delete refused")
        body = (await mem.post(self.PATH)).json()
        assert any("Delete failed" in e for e in body["report"]["errors"])

    async def test_communities_are_counted(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="A"),
            node(uuid="n-2", name="B"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        assert (await mem.post(self.PATH)).json()["report"]["communities_found"] == 1

    async def test_an_edge_to_an_unknown_node_is_ignored(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name=f"kwami_{USER_ID}")]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-missing")]
        assert (await mem.post(self.PATH)).json()["report"]["communities_found"] == 1

    @pytest.mark.parametrize("failing", ["edge", "node"])
    async def test_a_failing_listing_deletes_nothing(self, mem, zep, failing):
        """With no edge list every node looks orphaned; the pass must stop instead."""
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="Kept")]
        getattr(zep.graph, failing).get_by_user_id.error = RuntimeError("down")
        assert (await mem.post(self.PATH)).status_code == 500
        assert not zep.graph.node.delete.called

    @pytest.mark.parametrize("threshold", [79, 101])
    async def test_an_out_of_range_threshold_is_a_422(self, mem, threshold):
        r = await mem.post(self.PATH, params={"auto_merge_threshold": threshold})
        assert r.status_code == 422

    async def test_a_failure_is_a_500(self, monkeypatch, mem, zep, caplog):
        from src.api.routes.memory import analysis as mem_mod

        async def boom(client, user_id, max_items=0):
            raise RuntimeError("fetch exploded")

        monkeypatch.setattr(mem_mod, "_fetch_graph_raw", boom)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.post(self.PATH)).status_code == 500
        assert "Failed to reorganize graph" in caplog.text

    async def test_another_users_graph_is_a_403(self, mem, zep):
        assert (await mem.post("/memory/someone-else/reorganize")).status_code == 403
        assert not zep.graph.node.delete.called


class TestConnect:
    PATH = f"/memory/{USER_ID}/connect"

    def _body(self, **overrides):
        return {
            "source_node_uuid": "n-1",
            "target_node_uuid": "n-2",
            "relation": "KNOWS",
            **overrides,
        }

    async def test_it_creates_an_edge(self, mem, zep):
        zep.graph.node.get.result = node(uuid="n-1", name="Ada")
        body = (await mem.post(self.PATH, json=self._body())).json()
        assert body["success"] is True
        assert body["edge_uuid"] == "e-new"
        assert body["relation"] == "KNOWS"
        triple = zep.graph.add_fact_triple.last
        assert triple["source_node_uuid"] == "n-1"
        assert triple["target_node_uuid"] == "n-2"
        assert triple["fact_name"] == "KNOWS"

    async def test_the_fact_is_derived_from_the_relation(self, mem, zep):
        zep.graph.node.get.result = node(name="Ada")
        body = (await mem.post(self.PATH, json=self._body(relation="WORKS_AT"))).json()
        assert body["fact"] == "Ada works at Ada"

    async def test_an_explicit_fact_wins(self, mem, zep):
        zep.graph.node.get.result = node(name="Ada")
        body = (await mem.post(self.PATH, json=self._body(fact="Ada knows Grace"))).json()
        assert body["fact"] == "Ada knows Grace"

    async def test_an_unknown_source_node_is_a_404(self, mem, zep):
        zep.graph.node.get.error = RuntimeError("not found")
        r = await mem.post(self.PATH, json=self._body())
        assert r.status_code == 404
        assert "Source node n-1 not found" in r.json()["detail"]

    async def test_an_unknown_target_node_is_a_404(self, mem, zep):
        calls = {"n": 0}
        real_node = node(name="Ada")

        async def get(*, uuid_):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_node
            raise RuntimeError("not found")

        zep.graph.node.get = get
        r = await mem.post(self.PATH, json=self._body())
        assert r.status_code == 404
        assert "Target node n-2 not found" in r.json()["detail"]

    async def test_a_triple_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.node.get.result = node(name="Ada")
        zep.graph.add_fact_triple.error = RuntimeError("rejected")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.post(self.PATH, json=self._body())).status_code == 500
        assert "Failed to connect nodes" in caplog.text

    async def test_a_result_exposing_uuid_underscore(self, mem, zep):
        from types import SimpleNamespace

        zep.graph.node.get.result = node(name="Ada")
        zep.graph.add_fact_triple.result = SimpleNamespace(uuid_="e-alt")
        assert (await mem.post(self.PATH, json=self._body())).json()["edge_uuid"] == "e-alt"

    async def test_a_result_with_no_uuid(self, mem, zep):
        from types import SimpleNamespace

        zep.graph.node.get.result = node(name="Ada")
        zep.graph.add_fact_triple.result = SimpleNamespace()
        assert (await mem.post(self.PATH, json=self._body())).json()["edge_uuid"] is None

    async def test_a_none_result(self, mem, zep):
        zep.graph.node.get.result = node(name="Ada")
        zep.graph.add_fact_triple.result = None
        assert (await mem.post(self.PATH, json=self._body())).json()["edge_uuid"] is None

    async def test_another_users_graph_is_a_403(self, mem, zep):
        r = await mem.post("/memory/someone-else/connect", json=self._body())
        assert r.status_code == 403
        assert not zep.graph.add_fact_triple.called


class TestRemainingErrorPaths:
    """Outer handlers and duplicate-pair dedup, in the analysis routes."""

    async def test_the_graph_route_outer_handler(self, monkeypatch, mem, zep, caplog):
        from src.api.routes.memory import graph as mem_mod

        def boom(*a, **k):
            raise RuntimeError("len exploded")

        monkeypatch.setattr(mem_mod, "len", boom, raising=False)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.get(f"/memory/{USER_ID}/graph")
        assert r.status_code == 500
        assert "Failed to fetch memory graph" in caplog.text

    async def test_the_fact_rating_outer_handler(self, monkeypatch, mem, zep, caplog):
        """The inner `except` logs a warning; if that itself raises, the outer one runs."""
        from src.api.routes.memory import analysis as mem_mod

        real_warning = mem_mod.logger.warning

        def boom(*a, **k):
            raise RuntimeError("logging exploded")

        zep.graph.list_all.error = RuntimeError("graphs unavailable")
        monkeypatch.setattr(mem_mod.logger, "warning", boom)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.get(f"/memory/{USER_ID}/fact-rating")
        assert r.status_code == 500
        assert "Failed to get fact rating" in caplog.text
        assert real_warning is not None

    async def test_the_merge_outer_handler(self, monkeypatch, mem, zep, caplog):
        from src.api.routes.memory import analysis as mem_mod

        def boom(*a, **k):
            raise RuntimeError("getattr exploded")

        zep.graph.node.get.result = node(uuid="n-keep")
        monkeypatch.setattr(mem_mod, "getattr", boom, raising=False)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.post(
                f"/memory/{USER_ID}/merge", json={"keep_uuid": "n-keep", "remove_uuid": "n-remove"}
            )
        assert r.status_code == 500
        assert "Failed to merge nodes" in caplog.text

    async def test_the_reorganize_apply_outer_handler(self, monkeypatch, mem, zep, caplog):
        """Everything inside is individually wrapped, so the final log is the way in."""
        from src.api.routes.memory import analysis as mem_mod

        real_info = mem_mod.logger.info
        calls = {"n": 0}

        def boom(*a, **k):
            # The first info() is BEFORE the try; only the completion log is inside it.
            calls["n"] += 1
            if calls["n"] == 1:
                return real_info(*a, **k)
            raise RuntimeError("logging exploded")

        monkeypatch.setattr(mem_mod.logger, "info", boom)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.post(f"/memory/{USER_ID}/reorganize/apply", json={})
        assert r.status_code == 500
        assert "Failed to apply reorganization" in caplog.text

    @pytest.mark.parametrize("failing", ["edge", "node"])
    async def test_a_failing_listing_fails_the_analysis(self, mem, zep, failing):
        """Half a graph gives wrong communities and, worse, false orphans."""
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="A")]
        getattr(zep.graph, failing).get_by_user_id.error = RuntimeError("down")
        assert (await mem.get(f"/memory/{USER_ID}/communities")).status_code == 500

    async def test_a_uuid_listed_twice_is_one_node(self, mem, zep):
        """A node is not its own duplicate, however often a listing repeats it."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-1", name="Barcelona"),
        ]
        assert (await mem.get(f"/memory/{USER_ID}/duplicates")).json()["count"] == 0
        body = (await mem.post(f"/memory/{USER_ID}/reorganize/preview")).json()
        assert body["duplicates"] == []

    async def test_reorganize_skips_an_unrelated_edge_during_auto_merge(self, mem, zep):
        """The `else: continue` arm: an edge touching neither merging node."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        zep.ownership.edges["n-2"] = [
            edge(uuid="e-x", source="n-7", target="n-8"),
            edge(uuid="e-y", source="n-2", target="n-other"),
        ]
        await mem.post(f"/memory/{USER_ID}/reorganize")
        assert len(zep.graph.add_fact_triple.calls) == 1, "only the related edge"


class TestFinalBranches:
    """The last few arms: the delete-memory outer handler, the message-list
    shape fork, and the incoming-edge side of the one-click auto-merge."""

    async def test_the_delete_memory_outer_handler(self, monkeypatch, mem, zep, caplog):
        """Every inner step is wrapped; the completion log is the only way in."""
        from src.api.routes.memory import analysis as mem_mod

        real_info = mem_mod.logger.info
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            # The first info() is before the try; the completion log is inside it.
            if calls["n"] == 1:
                return real_info(*a, **k)
            raise RuntimeError("logging exploded")

        monkeypatch.setattr(mem_mod.logger, "info", boom)
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.delete(f"/memory/{USER_ID}")
        assert r.status_code == 500
        assert "Failed to delete user memory" in caplog.text

    async def test_a_message_response_that_is_neither_shape(self, mem, zep):
        """Not a MessageListResponse and not a list -- yields no messages, no crash."""
        from types import SimpleNamespace

        zep.user.get_threads.result = [
            SimpleNamespace(thread_id="t-1", uuid_="t-1", user_id=USER_ID, created_at=None)
        ]
        zep.thread.get.result = "an unexpected string"
        body = (await mem.get(f"/memory/{USER_ID}/messages")).json()
        assert body["session_count"] == 1
        assert body["message_count"] == 0

    async def test_auto_merge_repoints_an_incoming_edge(self, mem, zep):
        """The `elif et == remove` arm of the one-click reorganize."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Barcelona"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2")]
        zep.ownership.edges["n-2"] = [edge(source="n-other", target="n-2")]
        await mem.post(f"/memory/{USER_ID}/reorganize")
        triple = zep.graph.add_fact_triple.last
        assert triple["source_node_uuid"] == "n-other"
        assert triple["target_node_uuid"] == "n-1", "repointed onto the kept node"


class TestPartialBranches:
    """Loop-continuation and guard arms that a single-item fixture cannot reach.

    Each needs either a second iteration or a falsey field where the happy path
    always has a value -- an edge with no endpoints, a search that returns none.
    """

    async def test_entities_by_type_with_a_search_returning_no_edges(self, mem, zep):
        """`if edges_response and edges_response.edges` in the /edges fallback."""
        from types import SimpleNamespace

        zep.graph.edge.get_by_user_id.error = RuntimeError("down")
        zep.graph.search.result = SimpleNamespace(edges=None)
        assert (await mem.get(f"/memory/{USER_ID}/edges")).json()["edges"] == []

    async def test_a_community_member_missing_from_the_node_list(self, monkeypatch, mem, zep):
        """`if node_data` -- a uuid in the partition with no matching node row."""
        from src.api.routes.memory import analysis as mem_mod

        async def fetch(client, user_id, limit=200):
            return (
                [{"uuid": "n-1", "name": "A", "summary": None, "labels": []}],
                [
                    {
                        "source_node_uuid": "n-1",
                        "target_node_uuid": "n-1",
                        "uuid": "e1",
                        "fact": None,
                        "name": None,
                        "valid_at": None,
                        "invalid_at": None,
                    }
                ],
            )

        monkeypatch.setattr(mem_mod, "_fetch_graph_raw", fetch)

        def partition(graph, **kwargs):
            return {"n-1": 0, "n-ghost": 0}

        monkeypatch.setattr(mem_mod, "_fetch_graph_raw", fetch)
        import community.community_louvain as louvain

        monkeypatch.setattr(louvain, "best_partition", partition)
        body = (await mem.get(f"/memory/{USER_ID}/communities")).json()
        assert body["communities"][0]["size"] == 1, "the ghost uuid is dropped"

    @pytest.mark.parametrize(
        "path",
        [
            "/duplicates",
            "/reorganize/preview",
            "/reorganize",
        ],
    )
    async def test_an_edge_with_no_endpoints_is_tolerated(self, mem, zep, path):
        """`if src:` / `if e["source_node_uuid"]:` -- both endpoints null."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Quetzalcoatl"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source=None, target=None)]
        method = mem.get if path == "/duplicates" else mem.post
        r = await method(f"/memory/{USER_ID}{path}")
        assert r.status_code == 200

    async def test_an_edge_with_only_a_source(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Quetzalcoatl"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target=None)]
        assert (await mem.get(f"/memory/{USER_ID}/duplicates")).status_code == 200

    async def test_an_edge_with_only_a_target(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Barcelona"),
            node(uuid="n-2", name="Quetzalcoatl"),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source=None, target="n-2")]
        assert (await mem.get(f"/memory/{USER_ID}/duplicates")).status_code == 200
