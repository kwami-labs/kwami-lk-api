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
from src.core.security import AuthUser
from src.services.memory import (
    get_zep_client,
    iter_all_threads,
    thread_belongs_to,
    verify_user_access,
)

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()

# How many records are pulled from Zep before slicing a "page" in Python.
# Endpoints fetch this many regardless of the requested page size, so `total`
# saturates here and `has_more` is wrong beyond it.
ZEP_FETCH_LIMIT = 1000


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
        result["errors"].append(f"graph.search: {str(e)}")

    # Try graph nodes API
    try:
        nodes = await client.graph.node.get_by_user_id(user_id=user_id, limit=10)
        result["graph_nodes"] = len(nodes) if nodes else 0
        if nodes:
            result["node_names"] = [n.name for n in nodes if hasattr(n, "name")]
    except Exception as e:
        result["errors"].append(f"graph.node: {str(e)}")

    # Threads belonging to this user only.
    #
    # This endpoint used to return `result["all_threads"]` -- every thread in the
    # Zep project, with its owner's user_id -- to any authenticated caller. That
    # was a cross-tenant enumeration of the entire user base.
    try:
        async for thread in iter_all_threads(client):
            thread_id = getattr(thread, "thread_id", None) or getattr(thread, "uuid_", None)
            thread_user = getattr(thread, "user_id", None)
            if not thread_belongs_to(thread_id, thread_user, user_id):
                continue
            info = {"thread_id": thread_id, "user_id": thread_user}
            try:
                ctx = await client.thread.get_context(thread_id=thread_id)
                if ctx and ctx.context:
                    info["context"] = (
                        ctx.context[:500] + "..." if len(ctx.context) > 500 else ctx.context
                    )
            except Exception as ctx_err:
                info["context_error"] = str(ctx_err)[:100]
            result["threads"].append(info)
    except Exception as e:
        result["errors"].append(f"thread.list_all: {str(e)}")

    return result


@router.get("/{user_id}/facts")
async def get_user_facts(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get facts stored for a user via graph search (Zep v3) with pagination."""
    verify_user_access(user, user_id)
    try:
        # Zep v3 stores facts on graph edges - search for them
        facts_response = await client.graph.search(
            user_id=user_id,
            query="user information facts preferences",
            scope="edges",
            limit=ZEP_FETCH_LIMIT,
        )
        all_facts = []
        if facts_response and facts_response.edges:
            all_facts = [
                edge.fact for edge in facts_response.edges if hasattr(edge, "fact") and edge.fact
            ]

        # Apply pagination
        total = len(all_facts)
        paginated = all_facts[offset : offset + limit]
        has_more = (offset + limit) < total

        return {
            "facts": paginated,
            "count": len(paginated),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }
    except Exception as e:
        # Check for 404 (user not found)
        if "404" in str(e):
            return {
                "facts": [],
                "count": 0,
                "total": 0,
                "offset": offset,
                "limit": limit,
                "has_more": False,
            }
        logger.exception("Failed to fetch facts")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
                    deleted["errors"].append(f"Failed to delete thread {thread_id}: {str(e)}")
        except Exception as e:
            deleted["errors"].append(f"Failed to list threads: {str(e)}")

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
                deleted["errors"].append(f"Failed to delete user: {error_msg}")

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
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{user_id}/messages")
async def get_user_messages(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(100, ge=1, le=500),
):
    """Get conversation messages for a user from their threads/sessions.

    This retrieves actual conversation history stored in Zep threads,
    which is where chat messages are stored via memory.add().
    """
    verify_user_access(user, user_id)
    logger.info("💬 Fetching messages for user: %s", user_id)
    try:
        messages = []
        sessions = []

        # Get all threads and find ones belonging to this user
        try:
            async for t in iter_all_threads(client):
                thread_id = getattr(t, "thread_id", None) or getattr(t, "uuid_", None)
                thread_user = getattr(t, "user_id", None)

                # Exact ownership: the substring test returned other tenants'
                # messages whenever their thread id happened to contain this id.
                if thread_belongs_to(thread_id, thread_user, user_id):
                    sessions.append(
                        {
                            "thread_id": thread_id,
                            "user_id": thread_user,
                            "created_at": str(t.created_at)
                            if hasattr(t, "created_at") and t.created_at
                            else None,
                        }
                    )

                    # Get messages from this thread (Zep v3: thread.get returns messages)
                    try:
                        msgs_response = await client.thread.get(
                            thread_id=thread_id,
                            limit=limit,
                        )
                        # MessageListResponse has a .messages attribute
                        msg_list = None
                        if msgs_response:
                            if hasattr(msgs_response, "messages"):
                                msg_list = msgs_response.messages
                            elif isinstance(msgs_response, list):
                                msg_list = msgs_response

                        if msg_list:
                            for msg in msg_list:
                                messages.append(
                                    {
                                        "uuid": msg.uuid if hasattr(msg, "uuid") else None,
                                        "content": msg.content if hasattr(msg, "content") else None,
                                        "role": msg.role
                                        if hasattr(msg, "role")
                                        else (msg.role_type if hasattr(msg, "role_type") else None),
                                        "role_type": msg.role_type
                                        if hasattr(msg, "role_type")
                                        else None,
                                        "created_at": str(msg.created_at)
                                        if hasattr(msg, "created_at") and msg.created_at
                                        else None,
                                        "thread_id": thread_id,
                                    }
                                )
                    except Exception as msg_err:
                        logger.warning(
                            "💬 Failed to get messages from thread %s: %s", thread_id, msg_err
                        )
        except Exception as e:
            logger.warning("💬 thread.list_all failed: %s", e)

        # Sort messages by created_at (newest first)
        messages.sort(key=lambda x: x.get("created_at") or "", reverse=True)

        logger.info("💬 Found %s messages across %s sessions", len(messages), len(sessions))
        return {
            "messages": messages[:limit],  # Limit total messages
            "message_count": len(messages),
            "sessions": sessions,
            "session_count": len(sessions),
        }

    except Exception as e:
        logger.exception("Failed to fetch messages")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.patch("/{user_id}/edge/{edge_uuid}")
async def update_edge(
    user_id: str,
    edge_uuid: str,
    body: UpdateEdgeRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Update a specific edge (fact/relationship) in the knowledge graph.

    Strategy: fetch existing edge, delete it, recreate via add_fact_triple
    with the updated fields. Only provided fields are changed.
    """
    verify_user_access(user, user_id)
    logger.info("✏️ Updating edge %s for user: %s", edge_uuid, user_id)

    try:
        # 1. Fetch the existing edge
        try:
            old_edge = await client.graph.edge.get(uuid_=edge_uuid)
        except Exception as e:
            if "404" in str(e):
                raise HTTPException(status_code=404, detail=f"Edge {edge_uuid} not found") from e
            raise

        # 2. Build the updated fields (merge old + new)
        new_fact = body.fact if body.fact is not None else (old_edge.fact or "related")
        new_name = body.name if body.name is not None else (old_edge.name or "RELATED_TO")
        new_source = (
            body.source_node_uuid
            if body.source_node_uuid is not None
            else old_edge.source_node_uuid
        )
        new_target = (
            body.target_node_uuid
            if body.target_node_uuid is not None
            else old_edge.target_node_uuid
        )
        new_valid_at = (
            body.valid_at
            if body.valid_at is not None
            else (
                str(old_edge.valid_at)
                if hasattr(old_edge, "valid_at") and old_edge.valid_at
                else None
            )
        )
        new_invalid_at = (
            body.invalid_at
            if body.invalid_at is not None
            else (
                str(old_edge.invalid_at)
                if hasattr(old_edge, "invalid_at") and old_edge.invalid_at
                else None
            )
        )

        if not new_source or not new_target:
            raise HTTPException(
                status_code=400, detail="Edge must have both source and target node UUIDs"
            )

        # 3. Delete the old edge
        await client.graph.edge.delete(uuid_=edge_uuid)
        logger.info("✏️ Deleted old edge: %s", edge_uuid)

        # 4. Recreate via add_fact_triple
        triple_kwargs = {
            "fact": new_fact,
            "fact_name": new_name,
            "source_node_uuid": new_source,
            "target_node_uuid": new_target,
            "user_id": user_id,
        }
        if new_valid_at:
            triple_kwargs["valid_at"] = new_valid_at
        if new_invalid_at:
            triple_kwargs["invalid_at"] = new_invalid_at

        result = await client.graph.add_fact_triple(**triple_kwargs)

        new_edge_uuid = None
        if result and hasattr(result, "edge_uuid"):
            new_edge_uuid = result.edge_uuid
        elif result and hasattr(result, "uuid_"):
            new_edge_uuid = result.uuid_

        logger.info("✏️ Recreated edge as: %s", new_edge_uuid)
        return {
            "success": True,
            "old_edge_uuid": edge_uuid,
            "new_edge_uuid": new_edge_uuid,
            "fact": new_fact,
            "name": new_name,
            "source_node_uuid": new_source,
            "target_node_uuid": new_target,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update edge")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.patch("/{user_id}/node/{node_uuid}")
async def update_node(
    user_id: str,
    node_uuid: str,
    body: UpdateNodeRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Update a specific node (entity) in the knowledge graph.

    Strategy: fetch existing node and its edges, delete the node (which also
    deletes connected edges), then recreate node + all edges via add_fact_triple
    with the updated node fields.
    """
    verify_user_access(user, user_id)
    logger.info("✏️ Updating node %s for user: %s", node_uuid, user_id)

    try:
        # 1. Fetch the existing node
        try:
            old_node = await client.graph.node.get(uuid_=node_uuid)
        except Exception as e:
            if "404" in str(e):
                raise HTTPException(status_code=404, detail=f"Node {node_uuid} not found") from e
            raise

        # 2. Fetch all edges connected to this node
        try:
            connected_edges = await client.graph.node.get_edges(node_uuid=node_uuid)
        except Exception:
            connected_edges = []

        # 3. Build updated node fields
        new_name = body.name if body.name is not None else (old_node.name or "Unknown")
        new_summary = (
            body.summary if body.summary is not None else getattr(old_node, "summary", None)
        )

        # 4. Delete the old node (this cascades to edges)
        await client.graph.node.delete(uuid_=node_uuid)
        logger.info("✏️ Deleted old node: %s (had %s edges)", node_uuid, len(connected_edges or []))

        # 5. Recreate the node + edges via add_fact_triple
        new_node_uuid = None
        recreated_edges = 0

        if connected_edges:
            for edge in connected_edges:
                edge_source = getattr(edge, "source_node_uuid", None)
                edge_target = getattr(edge, "target_node_uuid", None)
                edge_fact = getattr(edge, "fact", "related")
                edge_name = getattr(edge, "name", "RELATED_TO")

                triple_kwargs = {
                    "fact": edge_fact or "related",
                    "fact_name": edge_name or "RELATED_TO",
                    "user_id": user_id,
                }

                # Determine which side of the edge is the updated node
                if edge_source == node_uuid:
                    triple_kwargs["source_node_name"] = new_name
                    if new_summary:
                        triple_kwargs["source_node_summary"] = new_summary
                    triple_kwargs["target_node_uuid"] = edge_target
                elif edge_target == node_uuid:
                    triple_kwargs["source_node_uuid"] = edge_source
                    triple_kwargs["target_node_name"] = new_name
                    if new_summary:
                        triple_kwargs["target_node_summary"] = new_summary
                else:
                    continue

                # Add temporal data if available
                if hasattr(edge, "valid_at") and edge.valid_at:
                    triple_kwargs["valid_at"] = str(edge.valid_at)
                if hasattr(edge, "invalid_at") and edge.invalid_at:
                    triple_kwargs["invalid_at"] = str(edge.invalid_at)

                try:
                    result = await client.graph.add_fact_triple(**triple_kwargs)
                    recreated_edges += 1
                    # Capture the new node UUID from the first recreated edge
                    if new_node_uuid is None and result:
                        if edge_source == node_uuid and hasattr(result, "source_node_uuid"):
                            new_node_uuid = result.source_node_uuid
                        elif edge_target == node_uuid and hasattr(result, "target_node_uuid"):
                            new_node_uuid = result.target_node_uuid
                except Exception as triple_err:
                    logger.warning("✏️ Failed to recreate edge: %s", triple_err)
        else:
            # Node has no edges, create a standalone fact to recreate it
            try:
                result = await client.graph.add_fact_triple(
                    fact=f"{new_name} exists",
                    fact_name="EXISTS",
                    source_node_name=new_name,
                    source_node_summary=new_summary or "",
                    target_node_name=new_name,
                    user_id=user_id,
                )
                if result and hasattr(result, "source_node_uuid"):
                    new_node_uuid = result.source_node_uuid
            except Exception as triple_err:
                logger.warning("✏️ Failed to recreate standalone node: %s", triple_err)

        logger.info("✏️ Recreated node as: %s with %s edges", new_node_uuid, recreated_edges)
        return {
            "success": True,
            "old_node_uuid": node_uuid,
            "new_node_uuid": new_node_uuid,
            "name": new_name,
            "summary": new_summary,
            "recreated_edges": recreated_edges,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update node")
        raise HTTPException(status_code=500, detail=str(e)) from e
