"""`/memory` — the graph view, fact rating, custom instructions and ingest.

`/graph` is what the app's memory visualisation renders. Two behaviours there
carry weight: a node is only drawn once per uuid, and any node left unconnected
is attached to the user node so nothing floats off-screen unreachable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.unit.api.memory.conftest import USER_ID, edge, node

pytestmark = pytest.mark.anyio


class TestGraph:
    PATH = f"/memory/{USER_ID}/graph"

    async def test_an_empty_graph(self, mem, zep):
        assert (await mem.get(self.PATH)).json() == {"nodes": [], "edges": []}

    async def test_nodes_and_edges_are_linked_by_uuid(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Ada", labels=["Person"]),
            node(uuid="n-2", name="Barcelona", labels=["Location"]),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2", name="LIVES_IN")]
        body = (await mem.get(self.PATH)).json()
        assert [n["label"] for n in body["nodes"]] == ["Ada", "Barcelona"]
        assert body["nodes"][0]["type"] == "person"
        assert body["edges"] == [
            {"source": "entity_0", "target": "entity_1", "relation": "LIVES_IN"}
        ]

    async def test_the_user_node_is_larger_and_relabelled(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(
                uuid="n-1", name=f"kwami_{USER_ID}", summary="Daniel is a user.", labels=["Entity"]
            )
        ]
        n = (await mem.get(self.PATH)).json()["nodes"][0]
        assert n["type"] == "user"
        assert n["val"] == 25
        assert n["label"] == "Daniel", "the raw kwami_ id is never shown"

    async def test_a_long_user_label_is_also_replaced(self, mem, zep):
        long_name = "x" * 40
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name=long_name, summary="Daniel is a user.", labels=["user"])
        ]
        assert (await mem.get(self.PATH)).json()["nodes"][0]["label"] == "Daniel"

    async def test_a_short_user_label_is_kept(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", name="Daniel", labels=["user"])]
        assert (await mem.get(self.PATH)).json()["nodes"][0]["label"] == "Daniel"

    async def test_non_user_nodes_are_smaller(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(labels=["Person"])]
        assert (await mem.get(self.PATH)).json()["nodes"][0]["val"] == 15

    async def test_orphans_are_attached_to_the_user_node(self, mem, zep):
        """Otherwise an unconnected node floats off-screen with no way back."""
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name=f"kwami_{USER_ID}", labels=["user"]),
            node(uuid="n-2", name="Orphan", labels=["Preference"]),
        ]
        body = (await mem.get(self.PATH)).json()
        assert body["edges"] == [
            {"source": "entity_0", "target": "entity_1", "relation": "related_to"}
        ]

    async def test_a_connected_node_is_not_re_attached(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name=f"kwami_{USER_ID}", labels=["user"]),
            node(uuid="n-2", name="Connected", labels=["Preference"]),
        ]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-2", name="PREFERS")]
        body = (await mem.get(self.PATH)).json()
        assert len(body["edges"]) == 1
        assert body["edges"][0]["relation"] == "PREFERS"

    async def test_without_a_user_node_orphans_stay_orphans(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", labels=["Preference"])]
        assert (await mem.get(self.PATH)).json()["edges"] == []

    async def test_a_duplicate_edge_is_drawn_once(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", labels=["Person"]),
            node(uuid="n-2", labels=["Person"]),
        ]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid="e-1", source="n-1", target="n-2", name="KNOWS"),
            edge(uuid="e-2", source="n-1", target="n-2", name="KNOWS"),
        ]
        assert len((await mem.get(self.PATH)).json()["edges"]) == 1

    async def test_the_same_pair_with_a_different_relation_is_drawn_twice(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", labels=["Person"]),
            node(uuid="n-2", labels=["Person"]),
        ]
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid="e-1", source="n-1", target="n-2", name="KNOWS"),
            edge(uuid="e-2", source="n-1", target="n-2", name="WORKS_WITH"),
        ]
        assert len((await mem.get(self.PATH)).json()["edges"]) == 2

    async def test_a_self_loop_is_dropped(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", labels=["Person"])]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-1")]
        assert (await mem.get(self.PATH)).json()["edges"] == []

    async def test_an_edge_to_an_unknown_node_is_dropped(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", labels=["Person"])]
        zep.graph.edge.get_by_user_id.result = [edge(source="n-1", target="n-missing")]
        assert (await mem.get(self.PATH)).json()["edges"] == []

    async def test_a_node_without_a_uuid_is_still_drawn(self, mem, zep):
        bare = node(labels=["Person"])
        del bare.uuid_
        del bare.uuid
        zep.graph.node.get_by_user_id.result = [bare]
        body = (await mem.get(self.PATH)).json()
        assert len(body["nodes"]) == 1
        assert body["nodes"][0]["uuid"] is None

    async def test_it_falls_back_to_search_for_edges(self, mem, zep, caplog):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", labels=["Person"]),
            node(uuid="n-2", labels=["Person"]),
        ]
        zep.graph.edge.get_by_user_id.error = RuntimeError("edge api down")
        zep.graph.search.result = SimpleNamespace(
            edges=[edge(source="n-1", target="n-2", name="KNOWS")]
        )
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert len(body["edges"]) == 1
        assert "graph.edge failed" in caplog.text

    async def test_both_edge_paths_failing_still_returns_nodes(self, mem, zep, caplog):
        zep.graph.node.get_by_user_id.result = [node(uuid="n-1", labels=["Person"])]
        zep.graph.edge.get_by_user_id.error = RuntimeError("down")
        zep.graph.search.error = RuntimeError("also down")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert len(body["nodes"]) == 1
        assert "graph.search edges also failed" in caplog.text

    async def test_a_failing_node_fetch_yields_an_empty_graph(self, mem, zep, caplog):
        zep.graph.node.get_by_user_id.error = RuntimeError("nodes down")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert body["nodes"] == []
        assert "graph.node failed" in caplog.text

    async def test_an_empty_search_fallback(self, mem, zep):
        zep.graph.edge.get_by_user_id.error = RuntimeError("down")
        zep.graph.search.result = SimpleNamespace(edges=[])
        assert (await mem.get(self.PATH)).json()["edges"] == []

    @pytest.mark.parametrize("limit", [0, 5001])
    async def test_an_out_of_range_limit_is_a_422(self, mem, limit):
        assert (await mem.get(self.PATH, params={"limit": limit})).status_code == 422

    async def test_another_users_graph_is_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/graph")).status_code == 403


class TestFactRating:
    PATH = f"/memory/{USER_ID}/fact-rating"

    async def test_a_configured_rating_is_returned(self, mem, zep):
        zep.graph.list_all.result = SimpleNamespace(
            graphs=[
                SimpleNamespace(
                    graph_id=USER_ID,
                    fact_rating_instruction=SimpleNamespace(
                        instruction="Rate by usefulness",
                        examples=SimpleNamespace(high="h", medium="m", low="l"),
                    ),
                )
            ]
        )
        body = (await mem.get(self.PATH)).json()
        assert body["configured"] is True
        assert body["instruction"] == "Rate by usefulness"
        assert body["examples"] == {"high": "h", "medium": "m", "low": "l"}

    async def test_a_rating_without_examples(self, mem, zep):
        zep.graph.list_all.result = SimpleNamespace(
            graphs=[
                SimpleNamespace(
                    graph_id=USER_ID,
                    fact_rating_instruction=SimpleNamespace(instruction="x", examples=None),
                )
            ]
        )
        assert (await mem.get(self.PATH)).json()["examples"] is None

    async def test_no_matching_graph_is_unconfigured(self, mem, zep):
        zep.graph.list_all.result = SimpleNamespace(
            graphs=[SimpleNamespace(graph_id="someone-else", fact_rating_instruction=None)]
        )
        assert (await mem.get(self.PATH)).json() == {
            "configured": False,
            "instruction": None,
            "examples": None,
        }

    async def test_a_graph_identified_by_uuid_is_matched(self, mem, zep):
        zep.graph.list_all.result = SimpleNamespace(
            graphs=[
                SimpleNamespace(
                    graph_id=None,
                    uuid=f"graph-{USER_ID}",
                    fact_rating_instruction=SimpleNamespace(instruction="x", examples=None),
                )
            ]
        )
        assert (await mem.get(self.PATH)).json()["configured"] is True

    async def test_a_graph_with_no_identifier_is_skipped(self, mem, zep):
        zep.graph.list_all.result = SimpleNamespace(
            graphs=[SimpleNamespace(graph_id=None, uuid=None, fact_rating_instruction=None)]
        )
        assert (await mem.get(self.PATH)).json()["configured"] is False

    async def test_a_null_graph_list(self, mem, zep):
        zep.graph.list_all.result = SimpleNamespace(graphs=None)
        assert (await mem.get(self.PATH)).json()["configured"] is False

    async def test_a_response_without_a_graphs_attribute(self, mem, zep):
        zep.graph.list_all.result = SimpleNamespace()
        assert (await mem.get(self.PATH)).json()["configured"] is False

    async def test_a_failing_listing_is_unconfigured_not_an_error(self, mem, zep, caplog):
        zep.graph.list_all.error = RuntimeError("graphs unavailable")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert body["configured"] is False
        assert "Could not read graph info" in caplog.text

    async def test_setting_a_rating_always_fails_known_bug(self, mem, zep, caplog):
        """`from zep_cloud import FactRatingExamples` no longer resolves.

        The installed zep-cloud exports neither `FactRatingExamples` nor
        `FactRatingInstruction`, so the import at the top of the handler raises
        before any request is made and this route returns 500 for every caller —
        it cannot succeed. The broad `except Exception` turns the ImportError
        into the same 500 an upstream failure would produce, which is why it has
        not been noticed.

        Pinned as it ships. Fixing it means finding the types' new home in the
        SDK (or building the payload as a plain dict) and inverting this test.
        """
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.put(
                self.PATH,
                json={
                    "instruction": "Rate by usefulness",
                    "examples": {"high": "h", "medium": "m", "low": "l"},
                },
            )
        assert r.status_code == 500
        assert "FactRatingExamples" in caplog.text
        assert not zep.graph.update.called, "it never reaches the client"

    async def test_the_handler_would_work_if_the_types_resolved(self, monkeypatch, mem, zep):
        """The rest of the handler, with the missing SDK types stubbed back in."""
        import sys
        import types as pytypes

        stub = pytypes.ModuleType("zep_cloud")
        real = sys.modules["zep_cloud"]
        for name in dir(real):
            setattr(stub, name, getattr(real, name))
        stub.FactRatingExamples = lambda **kw: dict(kw)
        stub.FactRatingInstruction = lambda **kw: dict(kw)
        monkeypatch.setitem(sys.modules, "zep_cloud", stub)

        r = await mem.put(
            self.PATH,
            json={
                "instruction": "Rate by usefulness",
                "examples": {"high": "h", "medium": "m", "low": "l"},
            },
        )
        assert r.status_code == 200
        assert r.json()["instruction"] == "Rate by usefulness"
        assert zep.graph.update.last["graph_id"] == USER_ID

    async def test_a_set_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.update.error = RuntimeError("rejected")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.put(
                self.PATH,
                json={"instruction": "x", "examples": {"high": "h", "medium": "m", "low": "l"}},
            )
        assert r.status_code == 500
        assert "Failed to set fact rating" in caplog.text

    async def test_another_users_rating_is_a_403(self, mem, zep):
        assert (await mem.get("/memory/someone-else/fact-rating")).status_code == 403
        r = await mem.put(
            "/memory/someone-else/fact-rating",
            json={"instruction": "x", "examples": {"high": "h", "medium": "m", "low": "l"}},
        )
        assert r.status_code == 403
        assert not zep.graph.update.called


class TestCustomInstructions:
    PATH = f"/memory/{USER_ID}/instructions"

    async def test_listing_instructions(self, mem, zep):
        zep.graph.list_custom_instructions.result = SimpleNamespace(
            instructions=[SimpleNamespace(name="tone", text="Be brief.")]
        )
        body = (await mem.get(self.PATH)).json()
        assert body["count"] == 1
        assert body["instructions"][0] == {"name": "tone", "text": "Be brief."}

    async def test_an_empty_list(self, mem, zep):
        assert (await mem.get(self.PATH)).json() == {"instructions": [], "count": 0}

    async def test_a_null_instruction_list(self, mem, zep):
        zep.graph.list_custom_instructions.result = SimpleNamespace(instructions=None)
        assert (await mem.get(self.PATH)).json()["count"] == 0

    async def test_a_response_without_the_attribute(self, mem, zep):
        zep.graph.list_custom_instructions.result = SimpleNamespace()
        assert (await mem.get(self.PATH)).json()["count"] == 0

    async def test_a_none_response(self, mem, zep):
        zep.graph.list_custom_instructions.result = None
        assert (await mem.get(self.PATH)).json()["count"] == 0

    async def test_a_listing_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.list_custom_instructions.error = RuntimeError("down")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.get(self.PATH)).status_code == 500
        assert "Failed to get custom instructions" in caplog.text

    async def test_adding_instructions(self, mem, zep):
        r = await mem.post(
            self.PATH,
            json={
                "instructions": [
                    {"name": "tone", "text": "Be brief."},
                    {"name": "style", "text": "Be warm."},
                ]
            },
        )
        assert r.status_code == 200
        assert r.json() == {"success": True, "added": 2}
        assert zep.graph.add_custom_instructions.last["user_ids"] == [USER_ID]

    async def test_adding_an_empty_list(self, mem, zep):
        r = await mem.post(self.PATH, json={"instructions": []})
        assert r.status_code == 200
        assert r.json()["added"] == 0

    async def test_an_add_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.add_custom_instructions.error = RuntimeError("rejected")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.post(self.PATH, json={"instructions": [{"name": "n", "text": "t"}]})
        assert r.status_code == 500
        assert "Failed to add custom instructions" in caplog.text

    async def test_deleting_all_instructions(self, mem, zep):
        r = await mem.delete(self.PATH)
        assert r.status_code == 200
        assert r.json() == {"success": True, "deleted": "all"}
        assert zep.graph.delete_custom_instructions.last["instruction_names"] is None

    async def test_deleting_named_instructions(self, mem, zep):
        r = await mem.delete(self.PATH, params={"names": " tone , style ,, "})
        assert r.status_code == 200
        assert r.json()["deleted"] == ["tone", "style"]
        assert zep.graph.delete_custom_instructions.last["instruction_names"] == ["tone", "style"]

    async def test_a_blank_names_param_deletes_all(self, mem, zep):
        r = await mem.delete(self.PATH, params={"names": ""})
        assert r.json()["deleted"] == "all"

    async def test_a_delete_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.delete_custom_instructions.error = RuntimeError("rejected")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.delete(self.PATH)).status_code == 500
        assert "Failed to delete custom instructions" in caplog.text

    async def test_another_users_instructions_are_a_403(self, mem, zep):
        assert (await mem.get("/memory/someone-else/instructions")).status_code == 403
        r = await mem.post("/memory/someone-else/instructions", json={"instructions": []})
        assert r.status_code == 403
        assert (await mem.delete("/memory/someone-else/instructions")).status_code == 403
        assert not zep.graph.add_custom_instructions.called


class TestIngest:
    PATH = f"/memory/{USER_ID}/ingest"

    async def test_ingesting_text(self, mem, zep):
        r = await mem.post(self.PATH, json={"data": "Ada likes coffee", "type": "text"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["episode_uuid"] == "ep-1"
        assert body["data_length"] == len("Ada likes coffee")
        assert body["type"] == "text"
        call = zep.graph.add.last
        assert call["user_id"] == USER_ID
        assert "source_description" not in call

    async def test_a_source_description_is_carried(self, mem, zep):
        await mem.post(
            self.PATH, json={"data": "x", "type": "text", "source_description": "imported from CSV"}
        )
        assert zep.graph.add.last["source_description"] == "imported from CSV"

    async def test_an_episode_exposing_plain_uuid(self, mem, zep):
        zep.graph.add.result = SimpleNamespace(uuid="ep-alt")
        assert (await mem.post(self.PATH, json={"data": "x"})).json()["episode_uuid"] == "ep-alt"

    async def test_an_episode_with_no_uuid(self, mem, zep):
        zep.graph.add.result = SimpleNamespace()
        assert (await mem.post(self.PATH, json={"data": "x"})).json()["episode_uuid"] is None

    async def test_a_none_episode(self, mem, zep):
        zep.graph.add.result = None
        assert (await mem.post(self.PATH, json={"data": "x"})).json()["episode_uuid"] is None

    async def test_an_ingest_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.add.error = RuntimeError("rejected")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.post(self.PATH, json={"data": "x"})).status_code == 500
        assert "Failed to ingest data" in caplog.text

    async def test_another_users_graph_is_a_403(self, mem, zep):
        r = await mem.post("/memory/someone-else/ingest", json={"data": "x"})
        assert r.status_code == 403
        assert not zep.graph.add.called
