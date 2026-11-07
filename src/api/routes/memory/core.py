"""Per-user reads, and the destructive single-resource operations.\n\nIncluding `DELETE /{user_id}`, which enumerates every thread in the project --\nsee `iter_all_threads` for why that enumeration has to be exhaustive.\n"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from zep_cloud.client import AsyncZep

from src.api.deps import require_auth
from src.api.routes.memory.schemas import (
    UpdateEdgeRequest,
    UpdateNodeRequest,
)
from src.core.errors import failure_kind
from src.core.security import AuthUser
from src.services.memory import (
    get_zep_client,
    iter_all_threads,
    require_edge,
    require_node,
    require_nodes,
    thread_belongs_to,
    verify_user_access,
)
from src.services.memory.paging import collect
from src.services.memory.threads import message_row, messages_of, thread_id_of, user_threads

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()


@router.get("/debug/{user_id}")
async def debug_user_memory(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Debug endpoint to check what memory exists for a user."""
    verify_user_access(user, user_id)
    result = {
        "user_id": user_id,
        "facts": [],
        "graph_edges": [],
        "graph_nodes": 0,
        "threads": [],
        "errors": [],
    }

    # Try to get facts via graph.search (Zep v3 method)
    try:
        facts_response = await client.graph.search(
            user_id=user_id,
            query="user information",
            scope="edges",
            limit=20,
        )
        if facts_response and facts_response.edges:
            result["facts"] = [
                edge.fact for edge in facts_response.edges if hasattr(edge, "fact") and edge.fact
            ]
            result["graph_edges"] = [
                {"fact": edge.fact, "name": edge.name if hasattr(edge, "name") else None}
                for edge in facts_response.edges
                if hasattr(edge, "fact")
            ]
    except Exception as e:
        logger.warning("Memory debug: graph.search failed: %s", e)
        result["errors"].append(f"graph.search: {failure_kind(e)}")

    # Try graph nodes API
    try:
        nodes = await client.graph.node.get_by_user_id(user_id=user_id, limit=10)
        result["graph_nodes"] = len(nodes) if nodes else 0
        if nodes:
            result["node_names"] = [n.name for n in nodes if hasattr(n, "name")]
    except Exception as e:
        logger.warning("Memory debug: graph.node failed: %s", e)
        result["errors"].append(f"graph.node: {failure_kind(e)}")

    # Threads belonging to this user only, from the user's own listing.
    #
    # This endpoint used to return `result["all_threads"]` -- every thread in the
    # Zep project, with its owner's user_id -- to any authenticated caller. That
    # was a cross-tenant enumeration of the entire user base. It also asked for
    # `thread.get_context`, which zep-cloud does not have, so every thread
    # carried a `context_error`; the call is `get_user_context`.
    try:
        for thread in await user_threads(client, user_id):
            thread_id = thread_id_of(thread)
            info = {"thread_id": thread_id, "user_id": getattr(thread, "user_id", None)}
            try:
                ctx = await client.thread.get_user_context(thread_id=thread_id)
                if ctx and ctx.context:
                    info["context"] = (
                        ctx.context[:500] + "..." if len(ctx.context) > 500 else ctx.context
                    )
            except Exception as ctx_err:
                logger.warning("Memory debug: context of %s failed: %s", thread_id, ctx_err)
                info["context_error"] = failure_kind(ctx_err)
            result["threads"].append(info)
    except Exception as e:
        logger.warning("Memory debug: user.get_threads failed: %s", e)
        result["errors"].append(f"user.get_threads: {failure_kind(e)}")

    return result


@router.get("/{user_id}/facts")
async def get_user_facts(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get every fact stored for a user, with pagination.

    This used to be a graph search for "user information facts preferences",
    which returns the 50 edges most similar to that phrase -- not the user's
    facts. It now lists the graph.
    """
    verify_user_access(user, user_id)
    try:
        edges = await collect(client.graph.edge.get_by_user_id, user_id)
    except Exception as e:
        # A user Zep has never seen has no facts yet.
        if "404" in str(e):
            edges = []
        else:
            logger.exception("Failed to fetch facts")
            raise HTTPException(status_code=500, detail="Failed to fetch facts") from e

    all_facts = [fact for edge in edges if (fact := getattr(edge, "fact", None))]
    total = len(all_facts)
    paginated = all_facts[offset : offset + limit]
    return {
        "facts": paginated,
        "count": len(paginated),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


@router.delete("/{user_id}")
async def delete_user_memory(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Delete all memory for a user (user, threads, and graph data).

    This is a destructive operation that removes:
    - All threads/sessions associated with the user
    - The user's knowledge graph (nodes and edges)
    - The user record itself

    Use with caution - this cannot be undone.
    Requires authentication when auth is enabled.
    """
    verify_user_access(user, user_id)
    logger.info("🗑️ Deleting all memory for user: %s", user_id)
    deleted = {"threads": 0, "user": False, "errors": []}

    try:
        # 1. Delete all threads belonging to this user
        try:
            async for t in iter_all_threads(client):
                thread_id = getattr(t, "thread_id", None) or getattr(t, "uuid_", None)
                thread_user = getattr(t, "user_id", None)

                # Exact ownership only. The previous substring test could delete
                # another tenant's threads, and the single-page listing meant a
                # "delete all my memory" request silently left data behind.
                if not thread_belongs_to(thread_id, thread_user, user_id):
                    continue
                try:
                    await client.thread.delete(thread_id=thread_id)
                    deleted["threads"] += 1
                    logger.info("🗑️ Deleted thread: %s", thread_id)
                except Exception as e:
                    logger.warning("Failed to delete thread %s: %s", thread_id, e)
                    deleted["errors"].append(
                        f"Failed to delete thread {thread_id}: {failure_kind(e)}"
                    )
        except Exception as e:
            logger.warning("Failed to list threads: %s", e)
            deleted["errors"].append(f"Failed to list threads: {failure_kind(e)}")

        # 2. Delete the user (this also deletes associated graph data in Zep)
        try:
            await client.user.delete(user_id=user_id)
            deleted["user"] = True
            logger.info("🗑️ Deleted user: %s", user_id)
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg:
                deleted["errors"].append(f"User {user_id} not found")
            else:
                logger.warning("Failed to delete user: %s", e)
                deleted["errors"].append(f"Failed to delete user: {failure_kind(e)}")

        logger.info("🗑️ Deletion complete: %s threads, user=%s", deleted["threads"], deleted["user"])
        return {
            "success": deleted["user"] or deleted["threads"] > 0,
            "user_id": user_id,
            "deleted_threads": deleted["threads"],
            "deleted_user": deleted["user"],
            "errors": deleted["errors"] if deleted["errors"] else None,
        }

    except Exception as e:
        logger.exception("Failed to delete user memory")
        raise HTTPException(status_code=500, detail="Failed to delete user memory") from e


@router.get("/{user_id}/messages")
async def get_user_messages(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(100, ge=1, le=500),
):
    """The user's most recent messages across sessions, newest first.

    Reads the user's own threads (`user.get_threads`) rather than every thread
    in the project, newest session first, and stops once it holds `limit`
    messages: an older session cannot hold a newer message. Each thread's
    *last* messages are read; this used to read each thread's first `limit`,
    so a long session showed its opening and hid how it ended.

    `GET /{user_id}/sessions` keeps sessions apart, which is what the panel wants.
    """
    verify_user_access(user, user_id)
    logger.info("💬 Fetching messages for user: %s", user_id)
    try:
        threads = await user_threads(client, user_id)
    except Exception as e:
        logger.warning("💬 user.get_threads failed: %s", e)
        threads = []

    messages: list[dict] = []
    sessions = []
    for thread in threads:
        thread_id = thread_id_of(thread)
        sessions.append(
            {
                "thread_id": thread_id,
                "user_id": getattr(thread, "user_id", None),
                "created_at": str(thread.created_at)
                if getattr(thread, "created_at", None)
                else None,
            }
        )
        if len(messages) >= limit:
            continue
        try:
            response = await client.thread.get(thread_id=thread_id, lastn=limit)
        except Exception as msg_err:
            logger.warning("💬 Failed to get messages from thread %s: %s", thread_id, msg_err)
            continue
        messages.extend(message_row(message, thread_id) for message in messages_of(response))

    messages.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    logger.info("💬 Found %s messages across %s sessions", len(messages), len(sessions))
    return {
        "messages": messages[:limit],
        "message_count": len(messages),
        "sessions": sessions,
        "session_count": len(sessions),
    }


@router.delete("/{user_id}/edge/{edge_uuid}")
async def delete_edge(
    user_id: str,
    edge_uuid: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Delete a specific edge (fact) from the user's knowledge graph.

    This permanently removes the fact/relationship from memory.
    """
    verify_user_access(user, user_id)
    await require_edge(client, user_id, edge_uuid)
    logger.info("🗑️ Deleting edge %s for user: %s", edge_uuid, user_id)

    try:
        await client.graph.edge.delete(uuid_=edge_uuid)
        logger.info("🗑️ Successfully deleted edge: %s", edge_uuid)
        return {"success": True, "deleted_edge": edge_uuid}
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            raise HTTPException(status_code=404, detail=f"Edge {edge_uuid} not found") from e
        logger.exception("Failed to delete edge")
        raise HTTPException(status_code=500, detail="Failed to delete edge") from e


@router.delete("/{user_id}/node/{node_uuid}")
async def delete_node(
    user_id: str,
    node_uuid: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Delete a specific node (entity) from the user's knowledge graph.

    Note: This will also delete all edges connected to this node.
    """
    verify_user_access(user, user_id)
    await require_node(client, user_id, node_uuid)
    logger.info("🗑️ Deleting node %s for user: %s", node_uuid, user_id)

    try:
        await client.graph.node.delete(uuid_=node_uuid)
        logger.info("🗑️ Successfully deleted node: %s", node_uuid)
        return {"success": True, "deleted_node": node_uuid}
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            raise HTTPException(status_code=404, detail=f"Node {node_uuid} not found") from e
        logger.exception("Failed to delete node")
        raise HTTPException(status_code=500, detail="Failed to delete node") from e


@router.patch("/{user_id}/edge/{edge_uuid}")
async def update_edge(
    user_id: str,
    edge_uuid: str,
    body: UpdateEdgeRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Update a fact in place: its text, relation name or validity window.

    Zep updates an edge where it stands, so it keeps its uuid, its episodes and
    its attributes. Only moving a fact to other nodes still means recreating it,
    since an edge's endpoints cannot change: the new edge is written first and
    the old one removed after, so a failure leaves the fact where it was.
    """
    verify_user_access(user, user_id)
    await require_edge(client, user_id, edge_uuid)
    # The new endpoints are written into this user's graph, so they must be its own.
    await require_nodes(
        client,
        user_id,
        [uuid for uuid in (body.source_node_uuid, body.target_node_uuid) if uuid],
    )
    logger.info("✏️ Updating edge %s for user: %s", edge_uuid, user_id)

    try:
        try:
            old_edge = await client.graph.edge.get(uuid_=edge_uuid)
        except Exception as e:
            if "404" in str(e):
                raise HTTPException(status_code=404, detail=f"Edge {edge_uuid} not found") from e
            raise

        old_source = getattr(old_edge, "source_node_uuid", None)
        old_target = getattr(old_edge, "target_node_uuid", None)
        new_source = body.source_node_uuid or old_source
        new_target = body.target_node_uuid or old_target
        if not new_source or not new_target:
            raise HTTPException(
                status_code=400, detail="Edge must have both source and target node UUIDs"
            )

        changes = {
            name: value
            for name, value in (
                ("fact", body.fact),
                ("name", body.name),
                ("valid_at", body.valid_at),
                ("invalid_at", body.invalid_at),
            )
            if value is not None
        }

        if (new_source, new_target) == (old_source, old_target):
            updated = await client.graph.edge.update(uuid_=edge_uuid, **changes)
            logger.info("✏️ Updated edge in place: %s", edge_uuid)
            return {
                "success": True,
                "old_edge_uuid": edge_uuid,
                "new_edge_uuid": edge_uuid,
                "fact": getattr(updated, "fact", None) or changes.get("fact", old_edge.fact),
                "name": getattr(updated, "name", None) or changes.get("name", old_edge.name),
                "source_node_uuid": new_source,
                "target_node_uuid": new_target,
            }

        triple_kwargs = {
            "fact": changes.get("fact", getattr(old_edge, "fact", None) or "related"),
            "fact_name": changes.get("name", getattr(old_edge, "name", None) or "RELATED_TO"),
            "source_node_uuid": new_source,
            "target_node_uuid": new_target,
            "user_id": user_id,
        }
        for field in ("valid_at", "invalid_at"):
            value = changes.get(field) or getattr(old_edge, field, None)
            if value:
                triple_kwargs[field] = str(value)
        attributes = getattr(old_edge, "attributes", None)
        if attributes:
            triple_kwargs["edge_attributes"] = dict(attributes)

        result = await client.graph.add_fact_triple(**triple_kwargs)
        new_edge_uuid = getattr(result, "edge_uuid", None) or getattr(result, "uuid_", None)
        await client.graph.edge.delete(uuid_=edge_uuid)
        logger.info("✏️ Moved edge %s to %s", edge_uuid, new_edge_uuid)
        return {
            "success": True,
            "old_edge_uuid": edge_uuid,
            "new_edge_uuid": new_edge_uuid,
            "fact": triple_kwargs["fact"],
            "name": triple_kwargs["fact_name"],
            "source_node_uuid": new_source,
            "target_node_uuid": new_target,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update edge")
        raise HTTPException(status_code=500, detail="Failed to update edge") from e


@router.patch("/{user_id}/node/{node_uuid}")
async def update_node(
    user_id: str,
    node_uuid: str,
    body: UpdateNodeRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Rename an entity, rewrite its summary or change its labels, in place.

    This used to delete the node and rebuild its edges from
    `graph.node.get_edges` -- which under-reports badly (0 of a live node's 9
    edges), so renaming an entity silently destroyed most of its facts and gave
    it a new uuid the app did not know about. Zep updates a node where it
    stands, edges and all. Labels were accepted and ignored; they now apply.
    """
    verify_user_access(user, user_id)
    await require_node(client, user_id, node_uuid)
    logger.info("✏️ Updating node %s for user: %s", node_uuid, user_id)

    changes = {
        name: value
        for name, value in (
            ("name", body.name),
            ("summary", body.summary),
            ("labels", body.labels),
        )
        if value is not None
    }
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to update")

    try:
        updated = await client.graph.node.update(uuid_=node_uuid, **changes)
    except Exception as e:
        if "404" in str(e):
            raise HTTPException(status_code=404, detail=f"Node {node_uuid} not found") from e
        logger.exception("Failed to update node")
        raise HTTPException(status_code=500, detail="Failed to update node") from e

    logger.info("✏️ Updated node in place: %s", node_uuid)
    return {
        "success": True,
        "old_node_uuid": node_uuid,
        "new_node_uuid": node_uuid,
        "name": getattr(updated, "name", None) or changes.get("name"),
        "summary": getattr(updated, "summary", None) or changes.get("summary"),
        "labels": list(getattr(updated, "labels", None) or changes.get("labels") or []),
        "recreated_edges": 0,
    }
