"""Reading the knowledge graph: edges, nodes, search, entities, the whole graph.

Zep returns at most 50 records per listing call, so every reader here walks the
uuid cursor (see `src.services.memory.paging`). The edge and node listings take
either the `cursor` they returned last time or the older `offset`; a cursor
costs one Zep call per 50 records, an offset re-walks everything before it.

A listing Zep fails to serve is a 502, not an empty page: "this memory is
empty" and "the memory service is down" must not look the same in the panel.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from zep_cloud.client import AsyncZep

from src.api.deps import require_auth
from src.core.security import AuthUser
from src.services.memory import (
    _extract_user_display_name,
    _infer_node_type,
    get_zep_client,
    verify_user_access,
)
from src.services.memory.paging import collect, page, uuid_of

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()

#: The most records one `/graph` read will assemble.
MAX_GRAPH_LIMIT = 5000

UNAVAILABLE = "Memory service unavailable"


def _when(record: Any, name: str) -> str | None:
    value = getattr(record, name, None)
    return str(value) if value else None


def _edge_row(edge: Any) -> dict:
    return {
        "uuid": uuid_of(edge),
        "fact": getattr(edge, "fact", None),
        "name": getattr(edge, "name", None),
        "source_node_uuid": getattr(edge, "source_node_uuid", None),
        "target_node_uuid": getattr(edge, "target_node_uuid", None),
        "created_at": _when(edge, "created_at"),
        "valid_at": _when(edge, "valid_at"),
        "invalid_at": _when(edge, "invalid_at"),
        "expired_at": _when(edge, "expired_at"),
    }


def _node_row(node: Any) -> dict:
    return {
        "uuid": uuid_of(node),
        "name": getattr(node, "name", None),
        "summary": getattr(node, "summary", None),
        "labels": list(getattr(node, "labels", None) or []),
        "created_at": _when(node, "created_at"),
        "degree": getattr(node, "degree", None),
    }


def _page_body(key: str, rows: list[dict], offset: int, limit: int, cursor: str | None) -> dict:
    """The page envelope the app reads: `total` is exact once `has_more` is false.

    Zep gives no count for a listing, so while more remain `total` is a lower
    bound -- what has been seen so far plus the one record that proves there is
    more. A client that walks to the end ends with the exact figure.
    """
    has_more = cursor is not None
    seen = offset + len(rows)
    return {
        key: rows,
        "count": len(rows),
        "total": seen + 1 if has_more else seen,
        "total_is_exact": not has_more,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_cursor": cursor,
    }


@router.get("/{user_id}/edges")
async def get_user_edges(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cursor: str | None = Query(None, description="`next_cursor` from the previous page"),
):
    """Get edges (facts with temporal data) for a user, a page at a time.

    Edges represent facts/relationships in the knowledge graph. Each edge includes:
    - fact: The relationship description
    - valid_at: When the fact became true
    - invalid_at: When the fact became false (if superseded)
    - created_at: When Zep learned the fact
    - expired_at: When Zep learned the fact was no longer true

    Pass the `next_cursor` of one page to get the next; `offset` still works.
    """
    verify_user_access(user, user_id)
    logger.info("🔗 Fetching edges for user: %s (offset=%s, limit=%s)", user_id, offset, limit)
    try:
        edges, next_cursor = await page(
            client.graph.edge.get_by_user_id,
            user_id,
            limit=limit,
            cursor=cursor,
            offset=0 if cursor else offset,
        )
    except Exception as e:
        logger.warning("🔗 graph.edge.get_by_user_id failed: %s", e)
        # graph.search has no cursor: it can stand in for one page, the last.
        try:
            facts_response = await client.graph.search(
                user_id=user_id, query="*", scope="edges", limit=limit
            )
        except Exception as search_err:
            logger.warning("🔗 Fallback graph.search also failed: %s", search_err)
            raise HTTPException(status_code=502, detail=UNAVAILABLE) from search_err
        edges = list(getattr(facts_response, "edges", None) or [])
        next_cursor = None

    rows = [_edge_row(edge) for edge in edges]
    logger.info("🔗 Returning %s edges (offset=%s)", len(rows), offset)
    return _page_body("edges", rows, offset, limit, next_cursor)


@router.get("/{user_id}/nodes")
async def get_user_nodes(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cursor: str | None = Query(None, description="`next_cursor` from the previous page"),
):
    """Get nodes (entities with summaries) for a user, a page at a time.

    Nodes represent entities in the knowledge graph. Each node includes:
    - name: The entity name
    - summary: AI-generated overview of the entity
    - labels: Type categorization
    - degree: How many facts it takes part in

    Pass the `next_cursor` of one page to get the next; `offset` still works.
    """
    verify_user_access(user, user_id)
    logger.info("🔵 Fetching nodes for user: %s (offset=%s, limit=%s)", user_id, offset, limit)
    try:
        nodes, next_cursor = await page(
            client.graph.node.get_by_user_id,
            user_id,
            limit=limit,
            cursor=cursor,
            offset=0 if cursor else offset,
        )
    except Exception as e:
        logger.warning("🔵 graph.node.get_by_user_id failed: %s", e)
        raise HTTPException(status_code=502, detail=UNAVAILABLE) from e

    rows = [_node_row(node) for node in nodes]
    logger.info("🔵 Returning %s nodes (offset=%s)", len(rows), offset)
    return _page_body("nodes", rows, offset, limit, next_cursor)


@router.get("/{user_id}/search")
async def search_graph(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    q: str = Query(..., description="Search query"),
    scope: str = Query("nodes", description="Search scope: 'nodes', 'edges', or 'both'"),
    entity_types: str | None = Query(None, description="Comma-separated entity types to filter by"),
    limit: int = Query(20, ge=1, le=100),
):
    """Search the knowledge graph with optional entity type filtering.

    This allows precise queries like "find all Preference entities about food"
    or "find Person entities named John".
    """
    verify_user_access(user, user_id)
    logger.info(
        "🔍 Searching graph for user %s: query='%s', scope=%s, types=%s",
        user_id,
        q,
        scope,
        entity_types,
    )

    try:
        results = {"nodes": [], "edges": [], "query": q, "scope": scope}

        # Parse entity types filter
        node_labels = None
        if entity_types:
            node_labels = [t.strip() for t in entity_types.split(",") if t.strip()]

        # Search nodes
        if scope in ("nodes", "both"):
            try:
                search_kwargs = {
                    "user_id": user_id,
                    "query": q,
                    "scope": "nodes",
                    "limit": limit,
                }
                if node_labels:
                    search_kwargs["node_labels"] = node_labels

                nodes_response = await client.graph.search(**search_kwargs)

                if nodes_response and nodes_response.nodes:
                    for node in nodes_response.nodes:
                        results["nodes"].append(
                            {
                                "name": getattr(node, "name", ""),
                                "type": node.labels[0].lower()
                                if hasattr(node, "labels") and node.labels
                                else "entity",
                                "labels": list(node.labels)
                                if hasattr(node, "labels") and node.labels
                                else [],
                                "summary": getattr(node, "summary", ""),
                                "uuid": getattr(node, "uuid_", None) or getattr(node, "uuid", None),
                                "score": getattr(node, "score", 0),
                            }
                        )
            except Exception as e:
                logger.warning("🔍 Node search failed: %s", e)

        # Search edges
        if scope in ("edges", "both"):
            try:
                edges_response = await client.graph.search(
                    user_id=user_id,
                    query=q,
                    scope="edges",
                    limit=limit,
                )

                if edges_response and edges_response.edges:
                    for edge in edges_response.edges:
                        results["edges"].append(
                            {
                                "fact": getattr(edge, "fact", ""),
                                "relation": getattr(edge, "name", "related_to"),
                                "uuid": getattr(edge, "uuid_", None) or getattr(edge, "uuid", None),
                                "score": getattr(edge, "score", 0),
                            }
                        )
            except Exception as e:
                logger.warning("🔍 Edge search failed: %s", e)

        results["node_count"] = len(results["nodes"])
        results["edge_count"] = len(results["edges"])

        return results

    except Exception as e:
        logger.exception("Failed to search graph")
        raise HTTPException(status_code=500, detail="Failed to search graph") from e


@router.get("/{user_id}/entities/{entity_type}")
async def get_entities_by_type(
    user_id: str,
    entity_type: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get entities of a specific type from the knowledge graph with pagination.

    Examples:
    - GET /memory/{user_id}/entities/Preference - Get all user preferences
    - GET /memory/{user_id}/entities/Person - Get all people mentioned
    - GET /memory/{user_id}/entities/Location - Get all locations
    """
    verify_user_access(user, user_id)
    logger.info("🏷️ Fetching %s entities for user: %s", entity_type, user_id)

    try:
        nodes = await collect(client.graph.node.get_by_user_id, user_id)
    except Exception as e:
        logger.warning("🏷️ graph.node.get_by_user_id failed: %s", e)
        raise HTTPException(status_code=502, detail=UNAVAILABLE) from e

    wanted = entity_type.lower()
    all_entities = []
    for node in nodes:
        node_labels = list(getattr(node, "labels", None) or [])
        if any(label.lower() == wanted for label in node_labels):
            all_entities.append(
                {
                    "name": getattr(node, "name", ""),
                    "type": node_labels[0],
                    "labels": node_labels,
                    "summary": getattr(node, "summary", ""),
                    "uuid": uuid_of(node),
                    "created_at": _when(node, "created_at"),
                }
            )

    total = len(all_entities)
    paginated = all_entities[offset : offset + limit]
    return {
        "entity_type": entity_type,
        "entities": paginated,
        "count": len(paginated),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


@router.get("/{user_id}/graph")
async def get_memory_graph(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(1000, ge=1, le=MAX_GRAPH_LIMIT),
):
    """Get the knowledge graph representation of the user's memory from Zep.

    Uses Zep entity labels when available, with smart keyword inference as fallback.
    Fetches up to `limit` edges and nodes (default 1000) to build the full graph.
    """
    verify_user_access(user, user_id)
    logger.info("📊 Fetching memory graph for user: %s (limit=%s)", user_id, limit)
    try:
        nodes = []
        edges = []
        graph_edges_raw = []  # Store raw edges for relationship building

        # 1. Edges and 2. nodes, walked past Zep's 50-record page.
        try:
            raw_edges = await collect(client.graph.edge.get_by_user_id, user_id, max_items=limit)
            raw_nodes = await collect(client.graph.node.get_by_user_id, user_id, max_items=limit)
        except Exception as e:
            logger.warning("📊 graph listing failed: %s", e)
            raise HTTPException(status_code=502, detail=UNAVAILABLE) from e
        logger.info("📊 Got %s edges and %s nodes", len(raw_edges), len(raw_nodes))

        for edge in raw_edges:
            graph_edges_raw.append(
                {
                    "fact": getattr(edge, "fact", None),
                    "relation": getattr(edge, "name", "related_to"),
                    "source_node": getattr(edge, "source_node_uuid", None),
                    "target_node": getattr(edge, "target_node_uuid", None),
                }
            )

        entity_nodes = []
        for node in raw_nodes:
            node_name = getattr(node, "name", "Unknown")
            node_labels = list(getattr(node, "labels", None) or [])
            node_summary = getattr(node, "summary", None)

            # Infer type using labels + keyword analysis
            node_type = _infer_node_type(node_name, node_summary, node_labels)

            # Detect the user/kwami node
            if node_type == "user" or "kwami_" in node_name.lower():
                node_type = "user"

            entity_nodes.append(
                {
                    "name": node_name,
                    "type": node_type,
                    "summary": node_summary,
                    "uuid": uuid_of(node),
                    "created_at": getattr(node, "created_at", None),
                    "labels": node_labels,
                }
            )

        # 3. Build the visualization graph
        # Map UUIDs to node IDs for edge building
        uuid_to_id = {}
        user_node_id = None

        # Add entity nodes with inferred types
        for i, entity in enumerate(entity_nodes):
            node_id = f"entity_{i}"
            if entity.get("uuid"):
                uuid_to_id[entity["uuid"]] = node_id

            # Track user node for potential edge connections
            if entity["type"] == "user":
                user_node_id = node_id

            # Use a readable label for user nodes instead of kwami_{uuid}
            label = entity["name"]
            if entity["type"] == "user" and ("kwami_" in label.lower() or len(label) > 30):
                label = _extract_user_display_name(entity, graph_edges_raw, entity_nodes, user)

            nodes.append(
                {
                    "id": node_id,
                    "label": label,
                    "type": entity["type"],
                    "summary": entity["summary"],
                    "uuid": entity["uuid"],
                    "created_at": entity["created_at"],
                    "labels": entity["labels"],
                    "val": 25 if entity["type"] == "user" else 15,
                }
            )

        # 4. Build edges from raw graph edges (actual relationships)
        edges_added = set()
        connected_nodes = set()  # Track which nodes are connected

        for raw_edge in graph_edges_raw:
            source_uuid = raw_edge.get("source_node")
            target_uuid = raw_edge.get("target_node")
            relation = raw_edge.get("relation", "related_to")

            source_id = uuid_to_id.get(source_uuid)
            target_id = uuid_to_id.get(target_uuid)

            if source_id and target_id and source_id != target_id:
                edge_key = f"{source_id}-{target_id}-{relation}"
                if edge_key not in edges_added:
                    edges.append({"source": source_id, "target": target_id, "relation": relation})
                    edges_added.add(edge_key)
                    connected_nodes.add(source_id)
                    connected_nodes.add(target_id)

        # 5. Connect orphan nodes to user node if we have one
        if user_node_id:
            for node in nodes:
                if node["id"] != user_node_id and node["id"] not in connected_nodes:
                    edges.append(
                        {"source": user_node_id, "target": node["id"], "relation": "related_to"}
                    )

        logger.info("📊 Final graph: %s nodes, %s edges", len(nodes), len(edges))
        return {"nodes": nodes, "edges": edges}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch memory graph")
        raise HTTPException(status_code=500, detail="Failed to fetch memory graph") from e
