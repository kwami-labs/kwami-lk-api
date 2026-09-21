"""`/memory` — the list, ontology, search and entity-type routes.

`/edges` is the one with a real fallback: if `graph.edge.get_by_user_id` fails it
retries through `graph.search`, so a degraded Zep still answers. Both paths build
the same shape, so both are asserted the same way.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.routes.memory import DEFAULT_EDGE_TYPES, DEFAULT_ENTITY_TYPES
from tests.unit.api.memory.conftest import USER_ID, edge, node

pytestmark = pytest.mark.anyio


class TestEdges:
    PATH = f"/memory/{USER_ID}/edges"

    async def test_it_returns_paginated_edges(self, mem, zep):
        zep.graph.edge.get_by_user_id.result = [
            edge(uuid=f"e-{i}", fact=f"fact {i}") for i in range(5)
        ]
        body = (await mem.get(self.PATH, params={"limit": 2, "offset": 1})).json()
        assert [e["uuid"] for e in body["edges"]] == ["e-1", "e-2"]
        assert body["total"] == 5
        assert body["has_more"] is True

    async def test_every_temporal_field_is_serialised(self, mem, zep):
        zep.graph.edge.get_by_user_id.result = [
            edge(valid_at="2026-01-01", invalid_at="2026-02-01", expired_at="2026-03-01")
        ]
        e = (await mem.get(self.PATH)).json()["edges"][0]
        assert e["valid_at"] == "2026-01-01"
        assert e["invalid_at"] == "2026-02-01"
        assert e["expired_at"] == "2026-03-01"
        assert e["created_at"] == "2026-03-01T00:00:00Z"

    async def test_absent_temporal_fields_are_null(self, mem, zep):
        zep.graph.edge.get_by_user_id.result = [edge()]
        e = (await mem.get(self.PATH)).json()["edges"][0]
        assert e["valid_at"] is None and e["invalid_at"] is None and e["expired_at"] is None

    async def test_an_edge_without_uuid_underscore_falls_back(self, mem, zep):
        bare = edge()
        del bare.uuid_
        zep.graph.edge.get_by_user_id.result = [bare]
        assert (await mem.get(self.PATH)).json()["edges"][0]["uuid"] == "e-1"

    async def test_an_edge_with_no_uuid_at_all(self, mem, zep):
        bare = edge()
        del bare.uuid_
        del bare.uuid
        zep.graph.edge.get_by_user_id.result = [bare]
        assert (await mem.get(self.PATH)).json()["edges"][0]["uuid"] is None

    async def test_an_empty_graph(self, mem, zep):
        body = (await mem.get(self.PATH)).json()
        assert body["edges"] == []
        assert body["total"] == 0

    async def test_a_none_response(self, mem, zep):
        zep.graph.edge.get_by_user_id.result = None
        assert (await mem.get(self.PATH)).json()["total"] == 0

    async def test_it_falls_back_to_search(self, mem, zep, caplog):
        """A degraded `edge.get_by_user_id` must not empty the client's graph view."""
        zep.graph.edge.get_by_user_id.error = RuntimeError("edge api down")
        zep.graph.search.result = SimpleNamespace(edges=[edge(fact="from search")])
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert body["edges"][0]["fact"] == "from search"
        assert "graph.edge.get_by_user_id failed" in caplog.text

    async def test_the_fallback_serialises_temporal_fields_too(self, mem, zep):
        zep.graph.edge.get_by_user_id.error = RuntimeError("down")
        zep.graph.search.result = SimpleNamespace(
            edges=[edge(valid_at="2026-01-01", expired_at="2026-03-01")]
        )
        e = (await mem.get(self.PATH)).json()["edges"][0]
        assert e["valid_at"] == "2026-01-01"
        assert e["expired_at"] == "2026-03-01"

    async def test_a_fallback_edge_without_uuid_underscore(self, mem, zep):
        zep.graph.edge.get_by_user_id.error = RuntimeError("down")
        bare = edge()
        del bare.uuid_
        zep.graph.search.result = SimpleNamespace(edges=[bare])
        assert (await mem.get(self.PATH)).json()["edges"][0]["uuid"] == "e-1"

    async def test_a_fallback_edge_with_no_uuid(self, mem, zep):
        zep.graph.edge.get_by_user_id.error = RuntimeError("down")
        bare = edge()
        del bare.uuid_
        del bare.uuid
        zep.graph.search.result = SimpleNamespace(edges=[bare])
        assert (await mem.get(self.PATH)).json()["edges"][0]["uuid"] is None

    async def test_both_paths_failing_yields_an_empty_result(self, mem, zep, caplog):
        zep.graph.edge.get_by_user_id.error = RuntimeError("edge api down")
        zep.graph.search.error = RuntimeError("search down")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert body["edges"] == []
        assert "Fallback graph.search also failed" in caplog.text

    async def test_an_empty_search_fallback(self, mem, zep):
        zep.graph.edge.get_by_user_id.error = RuntimeError("down")
        zep.graph.search.result = SimpleNamespace(edges=[])
        assert (await mem.get(self.PATH)).json()["edges"] == []

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
    async def test_out_of_range_pagination_is_a_422(self, mem, params):
        assert (await mem.get(self.PATH, params=params)).status_code == 422

    async def test_another_users_edges_are_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/edges")).status_code == 403


class TestNodes:
    PATH = f"/memory/{USER_ID}/nodes"

    async def test_it_returns_paginated_nodes(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid=f"n-{i}", name=f"Node {i}") for i in range(5)
        ]
        body = (await mem.get(self.PATH, params={"limit": 2, "offset": 3})).json()
        assert [n["uuid"] for n in body["nodes"]] == ["n-3", "n-4"]
        assert body["total"] == 5
        assert body["has_more"] is False

    async def test_a_node_is_fully_serialised(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(name="Ada", summary="A person", labels=["Person", "Entity"])
        ]
        n = (await mem.get(self.PATH)).json()["nodes"][0]
        assert n["name"] == "Ada"
        assert n["summary"] == "A person"
        assert n["labels"] == ["Person", "Entity"]
        assert n["created_at"] == "2026-03-01T00:00:00Z"

    async def test_a_node_without_labels(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(labels=[])]
        assert (await mem.get(self.PATH)).json()["nodes"][0]["labels"] == []

    async def test_a_node_without_uuid_underscore(self, mem, zep):
        bare = node()
        del bare.uuid_
        zep.graph.node.get_by_user_id.result = [bare]
        assert (await mem.get(self.PATH)).json()["nodes"][0]["uuid"] == "n-1"

    async def test_a_node_with_no_uuid(self, mem, zep):
        bare = node()
        del bare.uuid_
        del bare.uuid
        zep.graph.node.get_by_user_id.result = [bare]
        assert (await mem.get(self.PATH)).json()["nodes"][0]["uuid"] is None

    async def test_a_node_without_created_at(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(created_at=None)]
        assert (await mem.get(self.PATH)).json()["nodes"][0]["created_at"] is None

    async def test_a_failing_fetch_yields_an_empty_result(self, mem, zep, caplog):
        zep.graph.node.get_by_user_id.error = RuntimeError("nodes down")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH)).json()
        assert body["nodes"] == []
        assert "graph.node.get_by_user_id failed" in caplog.text

    async def test_a_none_response(self, mem, zep):
        zep.graph.node.get_by_user_id.result = None
        assert (await mem.get(self.PATH)).json()["total"] == 0

    async def test_another_users_nodes_are_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/nodes")).status_code == 403


class TestOntology:
    """`graph.get_ontology` does not exist on the installed Zep client.

    `AsyncGraphClient` exposes `set_ontology` but no getter, so `GET /ontology`
    raises AttributeError, the broad `except Exception` catches it, "404" is not
    in the message, and every caller gets a 500. The route cannot succeed today;
    `test_the_getter_does_not_exist_on_the_real_client_known_bug` pins that, and
    the rest of these cover the handler through the fake so the reshaping logic
    is still exercised and stays correct once the getter is restored.
    """

    PATH = f"/memory/{USER_ID}/ontology"

    async def test_the_getter_does_not_exist_on_the_real_client_known_bug(self):
        from zep_cloud.client import AsyncZep

        graph = AsyncZep(api_key="x").graph
        assert not hasattr(graph, "get_ontology"), "invert this test when it returns"
        assert hasattr(graph, "set_ontology"), "the setter does exist, which is the asymmetry"

    async def test_a_configured_ontology_is_returned(self, mem, zep):
        zep.graph.get_ontology.result = SimpleNamespace(
            entity_types=[SimpleNamespace(name="Preference", description="d")],
            edge_types=[SimpleNamespace(name="KNOWS", description="k")],
        )
        body = (await mem.get(self.PATH)).json()
        assert body["entity_types"] == [{"name": "Preference", "description": "d"}]
        assert body["edge_types"] == [{"name": "KNOWS", "description": "k"}]
        assert "is_default" not in body

    async def test_an_ontology_with_null_type_lists(self, mem, zep):
        zep.graph.get_ontology.result = SimpleNamespace(entity_types=None, edge_types=None)
        body = (await mem.get(self.PATH)).json()
        assert body["entity_types"] == []
        assert body["edge_types"] == []

    async def test_no_ontology_falls_back_to_the_defaults(self, mem, zep):
        zep.graph.get_ontology.result = None
        body = (await mem.get(self.PATH)).json()
        assert body["is_default"] is True
        assert body["entity_types"] == DEFAULT_ENTITY_TYPES

    async def test_a_404_also_falls_back_to_the_defaults(self, mem, zep):
        zep.graph.get_ontology.error = RuntimeError("status 404")
        body = (await mem.get(self.PATH)).json()
        assert body["is_default"] is True
        assert body["edge_types"] == DEFAULT_EDGE_TYPES

    async def test_another_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.get_ontology.error = RuntimeError("zep down")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.get(self.PATH)).status_code == 500
        assert "Failed to fetch ontology" in caplog.text

    async def test_setting_an_ontology(self, mem, zep):
        r = await mem.put(
            self.PATH,
            json={
                "entity_types": [{"name": "Preference", "description": "d"}],
                "edge_types": [{"name": "KNOWS", "description": "k"}],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["entity_types_count"] == 1
        assert body["edge_types_count"] == 1
        call = zep.graph.set_ontology.last
        assert call["user_ids"] == [USER_ID]
        assert "Preference" in call["entities"]
        assert "KNOWS" in call["edges"]

    async def test_setting_an_empty_ontology(self, mem, zep):
        r = await mem.put(self.PATH, json={"entity_types": [], "edge_types": []})
        assert r.status_code == 200
        assert r.json()["entity_types_count"] == 0

    async def test_a_set_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.set_ontology.error = RuntimeError("rejected")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            r = await mem.put(self.PATH, json={"entity_types": [], "edge_types": []})
        assert r.status_code == 500
        assert "Failed to set ontology" in caplog.text

    async def test_resetting_restores_the_defaults(self, mem, zep):
        r = await mem.post(f"{self.PATH}/reset")
        assert r.status_code == 200
        body = r.json()
        assert body["entity_types_count"] == len(DEFAULT_ENTITY_TYPES)
        assert body["edge_types_count"] == len(DEFAULT_EDGE_TYPES)
        assert body["message"] == "Ontology reset to defaults"
        assert set(zep.graph.set_ontology.last["entities"]) == {
            e["name"] for e in DEFAULT_ENTITY_TYPES
        }

    async def test_a_reset_failure_is_a_500(self, mem, zep, caplog):
        zep.graph.set_ontology.error = RuntimeError("rejected")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.post(f"{self.PATH}/reset")).status_code == 500
        assert "Failed to reset ontology" in caplog.text

    async def test_another_users_ontology_is_a_403(self, mem, zep):
        assert (await mem.get("/memory/someone-else/ontology")).status_code == 403
        assert (
            await mem.put(
                "/memory/someone-else/ontology", json={"entity_types": [], "edge_types": []}
            )
        ).status_code == 403
        assert (await mem.post("/memory/someone-else/ontology/reset")).status_code == 403
        assert not zep.graph.set_ontology.called


class TestSearch:
    PATH = f"/memory/{USER_ID}/search"

    async def test_node_scope_is_the_default(self, mem, zep):
        zep.graph.search.result = SimpleNamespace(
            nodes=[node(name="Ada", labels=["Person"])], edges=[]
        )
        body = (await mem.get(self.PATH, params={"q": "ada"})).json()
        assert body["scope"] == "nodes"
        assert body["query"] == "ada"
        assert body["nodes"][0]["name"] == "Ada"
        assert body["nodes"][0]["type"] == "person", "the first label, lowercased"
        assert body["node_count"] == 1
        assert body["edge_count"] == 0

    async def test_edge_scope(self, mem, zep):
        zep.graph.search.result = SimpleNamespace(
            nodes=[], edges=[edge(fact="likes pizza", name="PREFERS")]
        )
        body = (await mem.get(self.PATH, params={"q": "pizza", "scope": "edges"})).json()
        assert body["edges"][0]["fact"] == "likes pizza"
        assert body["edges"][0]["relation"] == "PREFERS"
        assert body["node_count"] == 0

    async def test_both_scopes_run_two_searches(self, mem, zep):
        zep.graph.search.result = SimpleNamespace(nodes=[node()], edges=[edge()])
        body = (await mem.get(self.PATH, params={"q": "x", "scope": "both"})).json()
        assert body["node_count"] == 1
        assert body["edge_count"] == 1
        assert len(zep.graph.search.calls) == 2

    async def test_entity_types_become_node_labels(self, mem, zep):
        await mem.get(self.PATH, params={"q": "x", "entity_types": " Person , Preference ,, "})
        assert zep.graph.search.last["node_labels"] == ["Person", "Preference"]

    async def test_no_entity_types_sends_no_labels(self, mem, zep):
        await mem.get(self.PATH, params={"q": "x"})
        assert "node_labels" not in zep.graph.search.last

    async def test_a_node_without_labels_is_typed_entity(self, mem, zep):
        zep.graph.search.result = SimpleNamespace(nodes=[node(labels=[])], edges=[])
        body = (await mem.get(self.PATH, params={"q": "x"})).json()
        assert body["nodes"][0]["type"] == "entity"
        assert body["nodes"][0]["labels"] == []

    async def test_a_failing_node_search_is_logged_not_raised(self, mem, zep, caplog):
        zep.graph.search.error = RuntimeError("search down")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH, params={"q": "x"})).json()
        assert body["nodes"] == []
        assert "Node search failed" in caplog.text

    async def test_a_failing_edge_search_is_logged_not_raised(self, mem, zep, caplog):
        zep.graph.search.error = RuntimeError("search down")
        with caplog.at_level("WARNING", logger="kwami-api.memory"):
            body = (await mem.get(self.PATH, params={"q": "x", "scope": "edges"})).json()
        assert body["edges"] == []
        assert "Edge search failed" in caplog.text

    async def test_an_empty_node_response(self, mem, zep):
        zep.graph.search.result = SimpleNamespace(nodes=None, edges=None)
        assert (await mem.get(self.PATH, params={"q": "x"})).json()["node_count"] == 0

    async def test_an_empty_edge_response(self, mem, zep):
        zep.graph.search.result = SimpleNamespace(nodes=None, edges=None)
        body = (await mem.get(self.PATH, params={"q": "x", "scope": "edges"})).json()
        assert body["edge_count"] == 0

    async def test_a_none_edge_response(self, mem, zep):
        zep.graph.search.result = None
        body = (await mem.get(self.PATH, params={"q": "x", "scope": "edges"})).json()
        assert body["edge_count"] == 0

    async def test_an_unknown_scope_searches_nothing(self, mem, zep):
        body = (await mem.get(self.PATH, params={"q": "x", "scope": "galaxies"})).json()
        assert body["node_count"] == 0 and body["edge_count"] == 0
        assert not zep.graph.search.called

    async def test_the_query_is_required(self, mem):
        assert (await mem.get(self.PATH)).status_code == 422

    @pytest.mark.parametrize("limit", [0, 101])
    async def test_an_out_of_range_limit_is_a_422(self, mem, limit):
        r = await mem.get(self.PATH, params={"q": "x", "limit": limit})
        assert r.status_code == 422

    async def test_another_users_graph_is_a_403(self, mem):
        r = await mem.get("/memory/someone-else/search", params={"q": "x"})
        assert r.status_code == 403


class TestEntitiesByType:
    def path(self, entity_type="Person"):
        return f"/memory/{USER_ID}/entities/{entity_type}"

    async def test_it_filters_by_label(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid="n-1", name="Ada", labels=["Person"]),
            node(uuid="n-2", name="Acme", labels=["Organization"]),
        ]
        body = (await mem.get(self.path())).json()
        assert body["entity_type"] == "Person"
        assert [e["name"] for e in body["entities"]] == ["Ada"]
        assert body["total"] == 1

    async def test_the_match_is_case_insensitive(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(labels=["PERSON"])]
        assert (await mem.get(self.path("person"))).json()["total"] == 1

    async def test_a_node_with_several_labels_matches_any(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(labels=["Entity", "Person"])]
        body = (await mem.get(self.path())).json()
        assert body["total"] == 1
        assert body["entities"][0]["type"] == "Entity", "the first label is the type"

    async def test_a_node_without_labels_never_matches(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [node(labels=[])]
        assert (await mem.get(self.path())).json()["total"] == 0

    async def test_it_paginates(self, mem, zep):
        zep.graph.node.get_by_user_id.result = [
            node(uuid=f"n-{i}", labels=["Person"]) for i in range(5)
        ]
        body = (await mem.get(self.path(), params={"limit": 2})).json()
        assert body["count"] == 2
        assert body["has_more"] is True

    async def test_an_empty_graph(self, mem, zep):
        assert (await mem.get(self.path())).json()["entities"] == []

    async def test_a_none_response(self, mem, zep):
        zep.graph.node.get_by_user_id.result = None
        assert (await mem.get(self.path())).json()["total"] == 0

    async def test_a_failing_fetch_is_a_500(self, mem, zep, caplog):
        zep.graph.node.get_by_user_id.error = RuntimeError("nodes down")
        with caplog.at_level("ERROR", logger="kwami-api.memory"):
            assert (await mem.get(self.path())).status_code == 500
        assert "Failed to fetch entities by type" in caplog.text

    async def test_another_users_entities_are_a_403(self, mem):
        assert (await mem.get("/memory/someone-else/entities/Person")).status_code == 403
