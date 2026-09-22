"""Reading the knowledge graph: edges, nodes, search, entities, the whole graph."""

from __future__ import annotations

import logging
from typing import Annotated

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

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()

# How many records are pulled from Zep before slicing a "page" in Python.
# Endpoints fetch this many regardless of the requested page size, so `total`
# saturates here and `has_more` is wrong beyond it.
ZEP_FETCH_LIMIT = 1000


@router.get("/{user_id}/edges")
async def get_user_edges(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get edges (facts with temporal data) for a user with pagination.

    Edges represent facts/relationships in the knowledge graph. Each edge includes:
    - fact: The relationship description
    - valid_at: When the fact became true
    - invalid_at: When the fact became false (if superseded)
    - created_at: When Zep learned the fact
    - expired_at: When Zep learned the fact was no longer true

    Supports offset/limit pagination. Returns total count and has_more flag.
    """
    verify_user_access(user, user_id)
    logger.info("🔗 Fetching edges for user: %s (offset=%s, limit=%s)", user_id, offset, limit)
    try:
        all_edges = []

        # Get all edges from Zep (large batch)
        try:
            edges_response = await client.graph.edge.get_by_user_id(
                user_id=user_id, limit=ZEP_FETCH_LIMIT
            )
            if edges_response:
                for edge in edges_response:
                    all_edges.append(
                        {
                            "uuid": edge.uuid_
                            if hasattr(edge, "uuid_")
                            else (edge.uuid if hasattr(edge, "uuid") else None),
                            "fact": edge.fact if hasattr(edge, "fact") else None,
                            "name": edge.name if hasattr(edge, "name") else None,
                            "source_node_uuid": edge.source_node_uuid
                            if hasattr(edge, "source_node_uuid")
                            else None,
                            "target_node_uuid": edge.target_node_uuid
                            if hasattr(edge, "target_node_uuid")
                            else None,
                            "created_at": str(edge.created_at)
                            if hasattr(edge, "created_at") and edge.created_at
                            else None,
                            "valid_at": str(edge.valid_at)
                            if hasattr(edge, "valid_at") and edge.valid_at
                            else None,
                            "invalid_at": str(edge.invalid_at)
                            if hasattr(edge, "invalid_at") and edge.invalid_at
                            else None,
                            "expired_at": str(edge.expired_at)
                            if hasattr(edge, "expired_at") and edge.expired_at
                            else None,
                        }
                    )
        except Exception as e:
            logger.warning("🔗 graph.edge.get_by_user_id failed: %s", e)
            # Fallback to graph.search
            try:
                facts_response = await client.graph.search(
                    user_id=user_id,
                    query="*",
                    scope="edges",
                    limit=ZEP_FETCH_LIMIT,
                )
                if facts_response and facts_response.edges:
                    for edge in facts_response.edges:
                        all_edges.append(
                            {
                                "uuid": edge.uuid_
                                if hasattr(edge, "uuid_")
                                else (edge.uuid if hasattr(edge, "uuid") else None),
                                "fact": edge.fact if hasattr(edge, "fact") else None,
                                "name": edge.name if hasattr(edge, "name") else None,
                                "source_node_uuid": edge.source_node_uuid
                                if hasattr(edge, "source_node_uuid")
                                else None,
                                "target_node_uuid": edge.target_node_uuid
                                if hasattr(edge, "target_node_uuid")
                                else None,
                                "created_at": str(edge.created_at)
                                if hasattr(edge, "created_at") and edge.created_at
                                else None,
                                "valid_at": str(edge.valid_at)
                                if hasattr(edge, "valid_at") and edge.valid_at
                                else None,
                                "invalid_at": str(edge.invalid_at)
                                if hasattr(edge, "invalid_at") and edge.invalid_at
                                else None,
                                "expired_at": str(edge.expired_at)
                                if hasattr(edge, "expired_at") and edge.expired_at
                                else None,
                            }
                        )
            except Exception as search_err:
                logger.warning("🔗 Fallback graph.search also failed: %s", search_err)

        # Apply pagination
        total = len(all_edges)
        paginated = all_edges[offset : offset + limit]
        has_more = (offset + limit) < total

        logger.info(
            "🔗 Found %s total edges, returning %s (offset=%s)", total, len(paginated), offset
        )
        return {
            "edges": paginated,
            "count": len(paginated),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

    except Exception as e:
        logger.exception("Failed to fetch edges")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{user_id}/nodes")
async def get_user_nodes(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get nodes (entities with summaries) for a user with pagination.

    Nodes represent entities in the knowledge graph. Each node includes:
    - name: The entity name
    - summary: AI-generated overview of the entity
    - labels: Type categorization

    Supports offset/limit pagination. Returns total count and has_more flag.
    """
    verify_user_access(user, user_id)
    logger.info("🔵 Fetching nodes for user: %s (offset=%s, limit=%s)", user_id, offset, limit)
    try:
        all_nodes = []

        try:
            nodes_response = await client.graph.node.get_by_user_id(
                user_id=user_id, limit=ZEP_FETCH_LIMIT
            )
            if nodes_response:
                for node in nodes_response:
                    all_nodes.append(
                        {
                            "uuid": node.uuid_
                            if hasattr(node, "uuid_")
                            else (node.uuid if hasattr(node, "uuid") else None),
                            "name": node.name if hasattr(node, "name") else None,
                            "summary": node.summary if hasattr(node, "summary") else None,
                            "labels": list(node.labels)
                            if hasattr(node, "labels") and node.labels
                            else [],
                            "created_at": str(node.created_at)
                            if hasattr(node, "created_at") and node.created_at
                            else None,
                        }
                    )
        except Exception as e:
            logger.warning("🔵 graph.node.get_by_user_id failed: %s", e)

        # Apply pagination
        total = len(all_nodes)
        paginated = all_nodes[offset : offset + limit]
        has_more = (offset + limit) < total

        logger.info(
            "🔵 Found %s total nodes, returning %s (offset=%s)", total, len(paginated), offset
        )
        return {
            "nodes": paginated,
            "count": len(paginated),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

    except Exception as e:
        logger.exception("Failed to fetch nodes")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        all_entities = []

        # Get all nodes and filter by type
        nodes_response = await client.graph.node.get_by_user_id(
            user_id=user_id,
            limit=ZEP_FETCH_LIMIT,
        )

        if nodes_response:
            for node in nodes_response:
                node_labels = list(node.labels) if hasattr(node, "labels") and node.labels else []
                # Check if the entity type matches (case-insensitive)
                if any(label.lower() == entity_type.lower() for label in node_labels):
                    all_entities.append(
                        {
                            "name": getattr(node, "name", ""),
                            "type": node_labels[0] if node_labels else "entity",
                            "labels": node_labels,
                            "summary": getattr(node, "summary", ""),
                            "uuid": getattr(node, "uuid_", None) or getattr(node, "uuid", None),
                            "created_at": str(node.created_at)
                            if hasattr(node, "created_at") and node.created_at
                            else None,
                        }
                    )

        # Apply pagination
        total = len(all_entities)
        paginated = all_entities[offset : offset + limit]
        has_more = (offset + limit) < total

        return {
            "entity_type": entity_type,
            "entities": paginated,
            "count": len(paginated),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

    except Exception as e:
        logger.exception("Failed to fetch entities by type")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{user_id}/graph")
async def get_memory_graph(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(1000, ge=1, le=5000),
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

        # 1. Get edges via graph API (for relationships)
        try:
            edges_response = await client.graph.edge.get_by_user_id(user_id=user_id, limit=limit)
            if edges_response:
                logger.info("📊 Got %s edges from graph.edge", len(edges_response))
                for edge in edges_response:
                    edge_data = {
                        "fact": getattr(edge, "fact", None),
                        "relation": getattr(edge, "name", "related_to"),
                        "source_node": getattr(edge, "source_node_uuid", None),
                        "target_node": getattr(edge, "target_node_uuid", None),
                    }
                    graph_edges_raw.append(edge_data)
        except Exception as e:
            logger.warning("📊 graph.edge failed, trying search: %s", e)
            # Fallback to graph.search
            try:
                facts_response = await client.graph.search(
                    user_id=user_id,
                    query="*",
                    scope="edges",
                    limit=limit,
                )
                if facts_response and facts_response.edges:
                    for edge in facts_response.edges:
                        edge_data = {
                            "fact": getattr(edge, "fact", None),
                            "relation": getattr(edge, "name", "related_to"),
                            "source_node": getattr(edge, "source_node_uuid", None),
                            "target_node": getattr(edge, "target_node_uuid", None),
                        }
                        graph_edges_raw.append(edge_data)
            except Exception as search_e:
                logger.warning("📊 graph.search edges also failed: %s", search_e)

        # 2. Get nodes (entities) from graph.node API
        entity_nodes = []

        try:
            nodes_response = await client.graph.node.get_by_user_id(user_id=user_id, limit=limit)
            if nodes_response:
                logger.info("📊 Got %s nodes from graph.node", len(nodes_response))
                for node in nodes_response:
                    node_name = getattr(node, "name", "Unknown")
                    node_labels = (
                        list(node.labels) if hasattr(node, "labels") and node.labels else []
                    )
                    node_uuid = getattr(node, "uuid_", None) or getattr(node, "uuid", None)
                    created_at = node.created_at if hasattr(node, "created_at") else None
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
                            "uuid": node_uuid,
                            "created_at": created_at,
                            "labels": node_labels,
                        }
                    )
        except Exception as e:
            logger.warning("📊 graph.node failed: %s", e)

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

    except Exception as e:
        logger.exception("Failed to fetch memory graph")
        raise HTTPException(status_code=500, detail=str(e)) from e
