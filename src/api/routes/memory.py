import logging
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from zep_cloud.client import AsyncZep

from src.core.config import settings
from src.api.deps import require_auth
from src.core.security import check_user_access, AuthUser

router = APIRouter()
logger = logging.getLogger("kwami-api.memory")


# =============================================================================
# Pydantic Models for Ontology
# =============================================================================

class EntityTypeDefinition(BaseModel):
    """Definition of an entity type for the knowledge graph."""
    name: str
    description: str


class EdgeTypeDefinition(BaseModel):
    """Definition of an edge (relationship) type for the knowledge graph."""
    name: str
    description: str


class OntologySchema(BaseModel):
    """Full ontology schema with entity and edge types."""
    entity_types: list[EntityTypeDefinition]
    edge_types: list[EdgeTypeDefinition]


# =============================================================================
# Pydantic Models for Memory Editing
# =============================================================================

class UpdateEdgeRequest(BaseModel):
    """Request body for updating an edge (fact/relationship)."""
    fact: Optional[str] = None
    name: Optional[str] = None
    source_node_uuid: Optional[str] = None
    target_node_uuid: Optional[str] = None
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None


class UpdateNodeRequest(BaseModel):
    """Request body for updating a node (entity)."""
    name: Optional[str] = None
    summary: Optional[str] = None
    labels: Optional[list[str]] = None


class FactRatingExamplesRequest(BaseModel):
    """Examples for fact rating (high/medium/low rated facts)."""
    high: str
    medium: str
    low: str


class FactRatingRequest(BaseModel):
    """Request body for setting fact rating instructions."""
    instruction: str
    examples: FactRatingExamplesRequest


class CustomInstructionRequest(BaseModel):
    """A single custom instruction."""
    name: str
    text: str


class CustomInstructionsBody(BaseModel):
    """Request body for adding custom instructions."""
    instructions: list[CustomInstructionRequest]


class DeleteInstructionsBody(BaseModel):
    """Request body for deleting custom instructions."""
    instruction_names: Optional[list[str]] = None


class IngestRequest(BaseModel):
    """Request body for re-ingesting data into the graph."""
    data: str
    type: str = "text"
    source_description: Optional[str] = None


class MergeNodesRequest(BaseModel):
    """Request body for merging two duplicate nodes."""
    keep_uuid: str
    remove_uuid: str


class ConnectNodesRequest(BaseModel):
    """Request body for connecting two nodes with a new edge."""
    source_node_uuid: str
    target_node_uuid: str
    relation: str
    fact: Optional[str] = None


class MergePair(BaseModel):
    """A pair of nodes to merge."""
    keep_uuid: str
    remove_uuid: str


class ReorganizeApplyRequest(BaseModel):
    """Request body to apply selected reorganization actions."""
    orphan_uuids: list[str] = []
    merge_pairs: list[MergePair] = []


# Default ontology for Kwami agents
DEFAULT_ENTITY_TYPES = [
    {"name": "Preference", "description": "User preferences, choices, opinions, or selections."},
    {"name": "Procedure", "description": "Multi-step instructions or workflows."},
    {"name": "Person", "description": "People mentioned in conversation."},
    {"name": "Organization", "description": "Companies, institutions, teams, or groups."},
    {"name": "Location", "description": "Physical places, cities, countries, venues."},
    {"name": "Event", "description": "Scheduled or past events, meetings, appointments."},
    {"name": "Project", "description": "Work projects, personal initiatives, creative endeavors."},
    {"name": "Topic", "description": "Subjects of interest or discussion themes."},
    {"name": "Product", "description": "Products, services, software, or tools."},
    {"name": "Skill", "description": "User skills, expertise, or competencies."},
    {"name": "Goal", "description": "User goals, objectives, or aspirations."},
]

DEFAULT_EDGE_TYPES = [
    {"name": "KNOWS", "description": "The user knows a person."},
    {"name": "WORKS_AT", "description": "Employment relationship with an organization."},
    {"name": "LIVES_IN", "description": "The user's residence or location."},
    {"name": "INTERESTED_IN", "description": "Topics or things the user is interested in."},
    {"name": "WORKING_ON", "description": "Projects the user is actively working on."},
    {"name": "HAS_SKILL", "description": "Skills the user possesses."},
    {"name": "WANTS_TO", "description": "Goals or desires the user has expressed."},
    {"name": "ATTENDED", "description": "Events the user has attended or will attend."},
    {"name": "USES", "description": "Products or tools the user uses."},
    {"name": "PREFERS", "description": "Preferences the user has expressed."},
]


def _build_ontology_models(
    entity_types: list[dict],
    edge_types: list[dict],
) -> tuple[dict, dict]:
    """Build Zep v3 SDK ontology models from dict definitions.
    
    Converts simple {name, description} dicts into EntityModel/EdgeModel classes
    required by the Zep v3 SDK's set_ontology method.
    """
    from zep_cloud.external_clients.ontology import EntityModel, EntityText, EdgeModel
    from zep_cloud import EntityEdgeSourceTarget
    from pydantic import Field
    
    entities = {}
    for et in entity_types:
        name = et["name"]
        desc = et.get("description", name)
        model_cls = type(name, (EntityModel,), {
            "__doc__": desc,
            "__annotations__": {"detail": EntityText},
            "detail": Field(description=desc, default=None),
        })
        entities[name] = model_cls
    
    edges = {}
    for edge in edge_types:
        name = edge["name"]
        desc = edge.get("description", name)
        model_cls = type(name, (EdgeModel,), {
            "__doc__": desc,
            "__annotations__": {"detail": EntityText},
            "detail": Field(description=desc, default=None),
        })
        edges[name] = (
            model_cls,
            [EntityEdgeSourceTarget(source="User")],
        )
    
    return entities, edges


async def get_zep_client():
    if not settings.zep_api_key:
        raise HTTPException(status_code=503, detail="Memory service not configured (ZEP_API_KEY missing)")
    return AsyncZep(api_key=settings.zep_api_key)


def verify_user_access(user: AuthUser, user_id: str):
    """Verify the authenticated user has access to the requested user_id."""
    if not check_user_access(user, user_id):
        raise HTTPException(
            status_code=403,
            detail="Access denied: You can only access your own memory data"
        )


def thread_belongs_to(thread_id: str | None, thread_user_id: str | None, user_id: str) -> bool:
    """Decide thread ownership with anchored matching.

    This used to be ``thread_user == user_id or user_id in str(thread_id)``. The
    substring test matched any thread whose id merely *contained* the caller's id,
    so a user could read -- and, from the delete endpoint, destroy -- another
    tenant's threads.

    Zep sets ``user_id`` on threads it owns, so that is authoritative when present.
    The id fallback exists only for older threads created before that, and is
    anchored to the start so it cannot match on a coincidental substring.
    """
    if thread_user_id:
        return thread_user_id == user_id
    if not thread_id:
        return False
    thread_id = str(thread_id)
    return thread_id == user_id or thread_id.startswith((f"{user_id}:", f"{user_id}_"))


async def iter_all_threads(client: AsyncZep, page_size: int = 100):
    """Yield every thread in the project, following pagination to exhaustion.

    Callers used to take a single ``list_all(page_size=50|100)`` page. For the
    delete endpoint that meant "delete all memory" silently left threads behind
    once a project exceeded one page -- which makes an erasure request a false
    claim, not just a bug.

    Zep's thread API has no per-user filter, so project-wide enumeration is
    unavoidable here; ownership is applied by the caller via `thread_belongs_to`.
    """
    page = 1
    seen = 0
    while True:
        response = await client.thread.list_all(page_number=page, page_size=page_size)
        threads = getattr(response, "threads", None) or []
        if not threads:
            return
        for thread in threads:
            yield thread
        seen += len(threads)
        total = getattr(response, "total_count", None)
        if len(threads) < page_size or (total is not None and seen >= total):
            return
        page += 1

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
        "errors": []
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
            result["facts"] = [edge.fact for edge in facts_response.edges if hasattr(edge, 'fact') and edge.fact]
            result["graph_edges"] = [
                {"fact": edge.fact, "name": edge.name if hasattr(edge, 'name') else None}
                for edge in facts_response.edges if hasattr(edge, 'fact')
            ]
    except Exception as e:
        result["errors"].append(f"graph.search: {str(e)}")
    
    # Try graph nodes API
    try:
        nodes = await client.graph.node.get_by_user_id(user_id=user_id, limit=10)
        result["graph_nodes"] = len(nodes) if nodes else 0
        if nodes:
            result["node_names"] = [n.name for n in nodes if hasattr(n, 'name')]
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
        ZEP_FETCH_LIMIT = 1000
        # Zep v3 stores facts on graph edges - search for them
        facts_response = await client.graph.search(
            user_id=user_id,
            query="user information facts preferences",
            scope="edges",
            limit=ZEP_FETCH_LIMIT,
        )
        all_facts = []
        if facts_response and facts_response.edges:
            all_facts = [edge.fact for edge in facts_response.edges if hasattr(edge, 'fact') and edge.fact]
        
        # Apply pagination
        total = len(all_facts)
        paginated = all_facts[offset:offset + limit]
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
            return {"facts": [], "count": 0, "total": 0, "offset": offset, "limit": limit, "has_more": False}
        logger.error(f"Failed to fetch facts: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
    logger.info(f"🗑️ Deleting all memory for user: {user_id}")
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
                    logger.info(f"🗑️ Deleted thread: {thread_id}")
                except Exception as e:
                    deleted["errors"].append(f"Failed to delete thread {thread_id}: {str(e)}")
        except Exception as e:
            deleted["errors"].append(f"Failed to list threads: {str(e)}")
        
        # 2. Delete the user (this also deletes associated graph data in Zep)
        try:
            await client.user.delete(user_id=user_id)
            deleted["user"] = True
            logger.info(f"🗑️ Deleted user: {user_id}")
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg:
                deleted["errors"].append(f"User {user_id} not found")
            else:
                deleted["errors"].append(f"Failed to delete user: {error_msg}")
        
        logger.info(f"🗑️ Deletion complete: {deleted['threads']} threads, user={deleted['user']}")
        return {
            "success": deleted["user"] or deleted["threads"] > 0,
            "user_id": user_id,
            "deleted_threads": deleted["threads"],
            "deleted_user": deleted["user"],
            "errors": deleted["errors"] if deleted["errors"] else None,
        }
        
    except Exception as e:
        logger.error(f"Failed to delete user memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"💬 Fetching messages for user: {user_id}")
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
                    sessions.append({
                        "thread_id": thread_id,
                        "user_id": thread_user,
                        "created_at": str(t.created_at) if hasattr(t, 'created_at') and t.created_at else None,
                    })
                        
                    # Get messages from this thread (Zep v3: thread.get returns messages)
                    try:
                        msgs_response = await client.thread.get(
                            thread_id=thread_id,
                            limit=limit,
                        )
                        # MessageListResponse has a .messages attribute
                        msg_list = None
                        if msgs_response:
                            if hasattr(msgs_response, 'messages'):
                                msg_list = msgs_response.messages
                            elif isinstance(msgs_response, list):
                                msg_list = msgs_response
                            
                        if msg_list:
                            for msg in msg_list:
                                messages.append({
                                    "uuid": msg.uuid if hasattr(msg, 'uuid') else None,
                                    "content": msg.content if hasattr(msg, 'content') else None,
                                    "role": msg.role if hasattr(msg, 'role') else (msg.role_type if hasattr(msg, 'role_type') else None),
                                    "role_type": msg.role_type if hasattr(msg, 'role_type') else None,
                                    "created_at": str(msg.created_at) if hasattr(msg, 'created_at') and msg.created_at else None,
                                    "thread_id": thread_id,
                                })
                    except Exception as msg_err:
                        logger.warning(f"💬 Failed to get messages from thread {thread_id}: {msg_err}")
        except Exception as e:
            logger.warning(f"💬 thread.list_all failed: {e}")
        
        # Sort messages by created_at (newest first)
        messages.sort(key=lambda x: x.get('created_at') or '', reverse=True)
        
        logger.info(f"💬 Found {len(messages)} messages across {len(sessions)} sessions")
        return {
            "messages": messages[:limit],  # Limit total messages
            "message_count": len(messages),
            "sessions": sessions,
            "session_count": len(sessions),
        }
        
    except Exception as e:
        logger.error(f"Failed to fetch messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"🗑️ Deleting edge {edge_uuid} for user: {user_id}")
    
    try:
        await client.graph.edge.delete(uuid_=edge_uuid)
        logger.info(f"🗑️ Successfully deleted edge: {edge_uuid}")
        return {"success": True, "deleted_edge": edge_uuid}
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            raise HTTPException(status_code=404, detail=f"Edge {edge_uuid} not found")
        logger.error(f"Failed to delete edge: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"🗑️ Deleting node {node_uuid} for user: {user_id}")
    
    try:
        await client.graph.node.delete(uuid_=node_uuid)
        logger.info(f"🗑️ Successfully deleted node: {node_uuid}")
        return {"success": True, "deleted_node": node_uuid}
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            raise HTTPException(status_code=404, detail=f"Node {node_uuid} not found")
        logger.error(f"Failed to delete node: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"✏️ Updating edge {edge_uuid} for user: {user_id}")
    
    try:
        # 1. Fetch the existing edge
        try:
            old_edge = await client.graph.edge.get(uuid_=edge_uuid)
        except Exception as e:
            if "404" in str(e):
                raise HTTPException(status_code=404, detail=f"Edge {edge_uuid} not found")
            raise
        
        # 2. Build the updated fields (merge old + new)
        new_fact = body.fact if body.fact is not None else (old_edge.fact or "related")
        new_name = body.name if body.name is not None else (old_edge.name or "RELATED_TO")
        new_source = body.source_node_uuid if body.source_node_uuid is not None else old_edge.source_node_uuid
        new_target = body.target_node_uuid if body.target_node_uuid is not None else old_edge.target_node_uuid
        new_valid_at = body.valid_at if body.valid_at is not None else (
            str(old_edge.valid_at) if hasattr(old_edge, 'valid_at') and old_edge.valid_at else None
        )
        new_invalid_at = body.invalid_at if body.invalid_at is not None else (
            str(old_edge.invalid_at) if hasattr(old_edge, 'invalid_at') and old_edge.invalid_at else None
        )
        
        if not new_source or not new_target:
            raise HTTPException(
                status_code=400,
                detail="Edge must have both source and target node UUIDs"
            )
        
        # 3. Delete the old edge
        await client.graph.edge.delete(uuid_=edge_uuid)
        logger.info(f"✏️ Deleted old edge: {edge_uuid}")
        
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
        if result and hasattr(result, 'edge_uuid'):
            new_edge_uuid = result.edge_uuid
        elif result and hasattr(result, 'uuid_'):
            new_edge_uuid = result.uuid_
        
        logger.info(f"✏️ Recreated edge as: {new_edge_uuid}")
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
        logger.error(f"Failed to update edge: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"✏️ Updating node {node_uuid} for user: {user_id}")
    
    try:
        # 1. Fetch the existing node
        try:
            old_node = await client.graph.node.get(uuid_=node_uuid)
        except Exception as e:
            if "404" in str(e):
                raise HTTPException(status_code=404, detail=f"Node {node_uuid} not found")
            raise
        
        # 2. Fetch all edges connected to this node
        try:
            connected_edges = await client.graph.node.get_edges(node_uuid=node_uuid)
        except Exception:
            connected_edges = []
        
        # 3. Build updated node fields
        new_name = body.name if body.name is not None else (old_node.name or "Unknown")
        new_summary = body.summary if body.summary is not None else getattr(old_node, 'summary', None)
        
        # 4. Delete the old node (this cascades to edges)
        await client.graph.node.delete(uuid_=node_uuid)
        logger.info(f"✏️ Deleted old node: {node_uuid} (had {len(connected_edges or [])} edges)")
        
        # 5. Recreate the node + edges via add_fact_triple
        new_node_uuid = None
        recreated_edges = 0
        
        if connected_edges:
            for edge in connected_edges:
                edge_source = getattr(edge, 'source_node_uuid', None)
                edge_target = getattr(edge, 'target_node_uuid', None)
                edge_fact = getattr(edge, 'fact', 'related')
                edge_name = getattr(edge, 'name', 'RELATED_TO')
                
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
                if hasattr(edge, 'valid_at') and edge.valid_at:
                    triple_kwargs["valid_at"] = str(edge.valid_at)
                if hasattr(edge, 'invalid_at') and edge.invalid_at:
                    triple_kwargs["invalid_at"] = str(edge.invalid_at)
                
                try:
                    result = await client.graph.add_fact_triple(**triple_kwargs)
                    recreated_edges += 1
                    # Capture the new node UUID from the first recreated edge
                    if new_node_uuid is None and result:
                        if edge_source == node_uuid and hasattr(result, 'source_node_uuid'):
                            new_node_uuid = result.source_node_uuid
                        elif edge_target == node_uuid and hasattr(result, 'target_node_uuid'):
                            new_node_uuid = result.target_node_uuid
                except Exception as triple_err:
                    logger.warning(f"✏️ Failed to recreate edge: {triple_err}")
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
                if result and hasattr(result, 'source_node_uuid'):
                    new_node_uuid = result.source_node_uuid
            except Exception as triple_err:
                logger.warning(f"✏️ Failed to recreate standalone node: {triple_err}")
        
        logger.info(f"✏️ Recreated node as: {new_node_uuid} with {recreated_edges} edges")
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
        logger.error(f"Failed to update node: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"🔗 Fetching edges for user: {user_id} (offset={offset}, limit={limit})")
    try:
        ZEP_FETCH_LIMIT = 1000
        all_edges = []
        
        # Get all edges from Zep (large batch)
        try:
            edges_response = await client.graph.edge.get_by_user_id(user_id=user_id, limit=ZEP_FETCH_LIMIT)
            if edges_response:
                for edge in edges_response:
                    all_edges.append({
                        "uuid": edge.uuid_ if hasattr(edge, 'uuid_') else (edge.uuid if hasattr(edge, 'uuid') else None),
                        "fact": edge.fact if hasattr(edge, 'fact') else None,
                        "name": edge.name if hasattr(edge, 'name') else None,
                        "source_node_uuid": edge.source_node_uuid if hasattr(edge, 'source_node_uuid') else None,
                        "target_node_uuid": edge.target_node_uuid if hasattr(edge, 'target_node_uuid') else None,
                        "created_at": str(edge.created_at) if hasattr(edge, 'created_at') and edge.created_at else None,
                        "valid_at": str(edge.valid_at) if hasattr(edge, 'valid_at') and edge.valid_at else None,
                        "invalid_at": str(edge.invalid_at) if hasattr(edge, 'invalid_at') and edge.invalid_at else None,
                        "expired_at": str(edge.expired_at) if hasattr(edge, 'expired_at') and edge.expired_at else None,
                    })
        except Exception as e:
            logger.warning(f"🔗 graph.edge.get_by_user_id failed: {e}")
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
                        all_edges.append({
                            "uuid": edge.uuid_ if hasattr(edge, 'uuid_') else (edge.uuid if hasattr(edge, 'uuid') else None),
                            "fact": edge.fact if hasattr(edge, 'fact') else None,
                            "name": edge.name if hasattr(edge, 'name') else None,
                            "source_node_uuid": edge.source_node_uuid if hasattr(edge, 'source_node_uuid') else None,
                            "target_node_uuid": edge.target_node_uuid if hasattr(edge, 'target_node_uuid') else None,
                            "created_at": str(edge.created_at) if hasattr(edge, 'created_at') and edge.created_at else None,
                            "valid_at": str(edge.valid_at) if hasattr(edge, 'valid_at') and edge.valid_at else None,
                            "invalid_at": str(edge.invalid_at) if hasattr(edge, 'invalid_at') and edge.invalid_at else None,
                            "expired_at": str(edge.expired_at) if hasattr(edge, 'expired_at') and edge.expired_at else None,
                        })
            except Exception as search_err:
                logger.warning(f"🔗 Fallback graph.search also failed: {search_err}")
        
        # Apply pagination
        total = len(all_edges)
        paginated = all_edges[offset:offset + limit]
        has_more = (offset + limit) < total
        
        logger.info(f"🔗 Found {total} total edges, returning {len(paginated)} (offset={offset})")
        return {
            "edges": paginated,
            "count": len(paginated),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }
        
    except Exception as e:
        logger.error(f"Failed to fetch edges: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"🔵 Fetching nodes for user: {user_id} (offset={offset}, limit={limit})")
    try:
        ZEP_FETCH_LIMIT = 1000
        all_nodes = []
        
        try:
            nodes_response = await client.graph.node.get_by_user_id(user_id=user_id, limit=ZEP_FETCH_LIMIT)
            if nodes_response:
                for node in nodes_response:
                    all_nodes.append({
                        "uuid": node.uuid_ if hasattr(node, 'uuid_') else (node.uuid if hasattr(node, 'uuid') else None),
                        "name": node.name if hasattr(node, 'name') else None,
                        "summary": node.summary if hasattr(node, 'summary') else None,
                        "labels": list(node.labels) if hasattr(node, 'labels') and node.labels else [],
                        "created_at": str(node.created_at) if hasattr(node, 'created_at') and node.created_at else None,
                    })
        except Exception as e:
            logger.warning(f"🔵 graph.node.get_by_user_id failed: {e}")
        
        # Apply pagination
        total = len(all_nodes)
        paginated = all_nodes[offset:offset + limit]
        has_more = (offset + limit) < total
        
        logger.info(f"🔵 Found {total} total nodes, returning {len(paginated)} (offset={offset})")
        return {
            "nodes": paginated,
            "count": len(paginated),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }
        
    except Exception as e:
        logger.error(f"Failed to fetch nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/ontology")
async def get_user_ontology(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Get the ontology (entity/edge type schema) for a user's knowledge graph.
    
    Returns the configured entity types and edge types that Zep uses
    for extracting information from conversations.
    """
    verify_user_access(user, user_id)
    logger.info(f"📋 Fetching ontology for user: {user_id}")
    
    try:
        ontology = await client.graph.get_ontology(user_id=user_id)
        
        if ontology:
            return {
                "user_id": user_id,
                "entity_types": [
                    {"name": e.name, "description": e.description}
                    for e in (ontology.entity_types or [])
                ],
                "edge_types": [
                    {"name": e.name, "description": e.description}
                    for e in (ontology.edge_types or [])
                ],
            }
        else:
            # Return defaults if no ontology configured
            return {
                "user_id": user_id,
                "entity_types": DEFAULT_ENTITY_TYPES,
                "edge_types": DEFAULT_EDGE_TYPES,
                "is_default": True,
            }
            
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            # No ontology configured, return defaults
            return {
                "user_id": user_id,
                "entity_types": DEFAULT_ENTITY_TYPES,
                "edge_types": DEFAULT_EDGE_TYPES,
                "is_default": True,
            }
        logger.error(f"Failed to fetch ontology: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{user_id}/ontology")
async def set_user_ontology(
    user_id: str,
    ontology: OntologySchema,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Set the ontology (entity/edge type schema) for a user's knowledge graph.
    
    This configures what types of entities and relationships Zep will
    extract from conversations. Changes apply to future extractions.
    """
    verify_user_access(user, user_id)
    logger.info(f"📋 Setting ontology for user: {user_id}")
    
    try:
        entities, edges = _build_ontology_models(
            [e.model_dump() for e in ontology.entity_types],
            [e.model_dump() for e in ontology.edge_types],
        )
        
        await client.graph.set_ontology(
            entities=entities,
            edges=edges,
            user_ids=[user_id],
        )
        
        logger.info(
            f"📋 Ontology set: {len(ontology.entity_types)} entity types, "
            f"{len(ontology.edge_types)} edge types"
        )
        
        return {
            "success": True,
            "user_id": user_id,
            "entity_types_count": len(ontology.entity_types),
            "edge_types_count": len(ontology.edge_types),
        }
        
    except Exception as e:
        logger.error(f"Failed to set ontology: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/ontology/reset")
async def reset_user_ontology(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Reset the ontology to default Kwami entity/edge types."""
    verify_user_access(user, user_id)
    logger.info(f"📋 Resetting ontology to defaults for user: {user_id}")
    
    try:
        entities, edges = _build_ontology_models(
            DEFAULT_ENTITY_TYPES,
            DEFAULT_EDGE_TYPES,
        )
        
        await client.graph.set_ontology(
            entities=entities,
            edges=edges,
            user_ids=[user_id],
        )
        
        return {
            "success": True,
            "user_id": user_id,
            "message": "Ontology reset to defaults",
            "entity_types_count": len(DEFAULT_ENTITY_TYPES),
            "edge_types_count": len(DEFAULT_EDGE_TYPES),
        }
        
    except Exception as e:
        logger.error(f"Failed to reset ontology: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/search")
async def search_graph(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    q: str = Query(..., description="Search query"),
    scope: str = Query("nodes", description="Search scope: 'nodes', 'edges', or 'both'"),
    entity_types: Optional[str] = Query(None, description="Comma-separated entity types to filter by"),
    limit: int = Query(20, ge=1, le=100),
):
    """Search the knowledge graph with optional entity type filtering.
    
    This allows precise queries like "find all Preference entities about food"
    or "find Person entities named John".
    """
    verify_user_access(user, user_id)
    logger.info(f"🔍 Searching graph for user {user_id}: query='{q}', scope={scope}, types={entity_types}")
    
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
                        results["nodes"].append({
                            "name": getattr(node, 'name', ''),
                            "type": node.labels[0].lower() if hasattr(node, 'labels') and node.labels else 'entity',
                            "labels": list(node.labels) if hasattr(node, 'labels') and node.labels else [],
                            "summary": getattr(node, 'summary', ''),
                            "uuid": getattr(node, 'uuid_', None) or getattr(node, 'uuid', None),
                            "score": getattr(node, 'score', 0),
                        })
            except Exception as e:
                logger.warning(f"🔍 Node search failed: {e}")
        
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
                        results["edges"].append({
                            "fact": getattr(edge, 'fact', ''),
                            "relation": getattr(edge, 'name', 'related_to'),
                            "uuid": getattr(edge, 'uuid_', None) or getattr(edge, 'uuid', None),
                            "score": getattr(edge, 'score', 0),
                        })
            except Exception as e:
                logger.warning(f"🔍 Edge search failed: {e}")
        
        results["node_count"] = len(results["nodes"])
        results["edge_count"] = len(results["edges"])
        
        return results
        
    except Exception as e:
        logger.error(f"Failed to search graph: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    logger.info(f"🏷️ Fetching {entity_type} entities for user: {user_id}")
    
    try:
        ZEP_FETCH_LIMIT = 1000
        all_entities = []
        
        # Get all nodes and filter by type
        nodes_response = await client.graph.node.get_by_user_id(
            user_id=user_id,
            limit=ZEP_FETCH_LIMIT,
        )
        
        if nodes_response:
            for node in nodes_response:
                node_labels = list(node.labels) if hasattr(node, 'labels') and node.labels else []
                # Check if the entity type matches (case-insensitive)
                if any(label.lower() == entity_type.lower() for label in node_labels):
                    all_entities.append({
                        "name": getattr(node, 'name', ''),
                        "type": node_labels[0] if node_labels else 'entity',
                        "labels": node_labels,
                        "summary": getattr(node, 'summary', ''),
                        "uuid": getattr(node, 'uuid_', None) or getattr(node, 'uuid', None),
                        "created_at": str(node.created_at) if hasattr(node, 'created_at') and node.created_at else None,
                    })
        
        # Apply pagination
        total = len(all_entities)
        paginated = all_entities[offset:offset + limit]
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
        logger.error(f"Failed to fetch entities by type: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _infer_node_type(name: str, summary: Optional[str], labels: list[str]) -> str:
    """Infer node type from Zep labels or fall back to keyword-based inference.
    
    Zep's free tier often returns generic labels, so we use smart inference.
    """
    # First, check if Zep provided a meaningful label
    if labels:
        label = labels[0].lower()
        # If it's a specific type (not generic), use it
        if label not in ("entity", "node", "unknown", ""):
            return label
    
    # Fall back to keyword-based inference
    name_lower = name.lower()
    text = f"{name_lower} {(summary or '').lower()}"
    
    # User/Assistant detection (highest priority)
    if "kwami_" in name_lower or name_lower == "user" or "identifies_as" in text:
        return "user"
    if name_lower in ("assistant", "ai", "bot") or "assistant" in name_lower:
        return "assistant"
    
    # Person detection
    person_indicators = ["person", "he ", "she ", "they ", "friend", "family", 
                        "brother", "sister", "mother", "father", "wife", "husband",
                        "colleague", "boss", "manager"]
    if any(p in text for p in person_indicators):
        return "person"
    
    # Pet/Animal detection
    pet_indicators = ["dog", "cat", "pet", "puppy", "kitten", "bird", "fish",
                     "labrador", "retriever", "shepherd", "poodle", "bulldog"]
    if any(p in text for p in pet_indicators):
        return "pet"
    
    # Location detection
    location_indicators = ["city", "country", "location", "lives in", "from", "born in",
                          "street", "address", "neighborhood", "district", "region",
                          "barcelona", "madrid", "london", "paris", "new york", "tokyo"]
    if any(loc in text for loc in location_indicators):
        return "location"
    
    # Place/Venue detection (more specific than location)
    place_indicators = ["park", "home", "house", "apartment", "office", "restaurant",
                       "café", "cafe", "bar", "gym", "school", "university", "hospital",
                       "store", "shop", "mall", "airport", "station"]
    if any(p in text for p in place_indicators):
        return "place"
    
    # Preference detection
    preference_indicators = ["likes", "loves", "enjoys", "prefers", "favorite", 
                            "favourite", "preference", "interested in", "passionate"]
    if any(p in text for p in preference_indicators):
        return "preference"
    
    # Skill/Profession detection
    skill_indicators = ["developer", "engineer", "designer", "artist", "musician",
                       "programmer", "software", "works as", "profession", "job",
                       "skill", "expertise", "experience in"]
    if any(s in text for s in skill_indicators):
        return "skill"
    
    # Topic/Interest detection
    topic_indicators = ["music", "sports", "art", "technology", "science", "cooking",
                       "gaming", "reading", "travel", "photography", "genre"]
    if any(t in text for t in topic_indicators):
        return "topic"
    
    # Event detection
    event_indicators = ["event", "meeting", "appointment", "birthday", "anniversary",
                       "conference", "party", "wedding", "trip", "vacation"]
    if any(e in text for e in event_indicators):
        return "event"
    
    # Project detection
    project_indicators = ["project", "working on", "building", "developing", "creating"]
    if any(p in text for p in project_indicators):
        return "project"
    
    # Product detection
    product_indicators = ["product", "app", "application", "tool", "service", "device"]
    if any(p in text for p in product_indicators):
        return "product"
    
    # Organization detection
    org_indicators = ["company", "organization", "team", "group", "corporation",
                     "startup", "business", "firm", "agency"]
    if any(o in text for o in org_indicators):
        return "organization"
    
    # Attribute/Property detection (colors, ages, etc.)
    if any(c in text for c in ["color", "colour", "brown", "black", "white", "red", "blue"]):
        return "attribute"
    if any(a in text for a in ["years old", "age", "height", "weight"]):
        return "attribute"
    
    # Default
    return "entity"


def _extract_user_display_name(
    user_entity: dict,
    graph_edges_raw: list[dict],
    all_nodes: list[dict],
    auth_user: AuthUser,
) -> str:
    """Extract the user's actual name from the knowledge graph data.
    
    Strategy (in order of priority):
    1. Check the user node's summary for a name (e.g. "Daniel is a...")
    2. Check edge facts for name-related info (e.g. "User's name is Daniel")
    3. Fall back to the auth user's email local part
    4. Fall back to "User"
    """
    import re
    
    # 1. Try to extract name from the user node summary
    summary = user_entity.get("summary") or ""
    if summary:
        # Common patterns: "X is a...", "X, also known as...", "X enjoys..."
        # The summary typically starts with the person's name
        first_sentence = summary.split(".")[0].strip()
        
        # Pattern: "Name is a/an ..." or "Name enjoys/likes/works..."
        match = re.match(r'^([A-Z][a-zà-ÿ]+(?:\s[A-Z][a-zà-ÿ]+)*)\s+(?:is|enjoys|likes|works|lives|has|was|prefers|loves|wants)', first_sentence)
        if match:
            name = match.group(1).strip()
            # Sanity check: not a generic word
            if name.lower() not in ("the", "this", "user", "person", "someone", "he", "she", "they"):
                return name
        
        # Pattern: "The user, Name, ..." or "The user Name ..."
        match = re.match(r'(?:The\s+)?user[,\s]+([A-Z][a-zà-ÿ]+(?:\s[A-Z][a-zà-ÿ]+)*)', first_sentence, re.IGNORECASE)
        if match:
            name = match.group(1).strip().rstrip(",")
            if name.lower() not in ("is", "has", "was", "the"):
                return name
    
    # 2. Check edge facts for name mentions
    user_uuid = user_entity.get("uuid")
    if user_uuid:
        for edge in graph_edges_raw:
            fact = (edge.get("fact") or "").lower()
            if edge.get("source_node") == user_uuid or edge.get("target_node") == user_uuid:
                # Look for "name is X", "called X", "named X", "identifies as X"
                for pattern in [r'name\s+is\s+([A-Z][a-zà-ÿ]+)', r'called\s+([A-Z][a-zà-ÿ]+)', 
                               r'named\s+([A-Z][a-zà-ÿ]+)', r'identifies\s+as\s+([A-Z][a-zà-ÿ]+)']:
                    match = re.search(pattern, edge.get("fact") or "", re.IGNORECASE)
                    if match:
                        return match.group(1).strip()
    
    # 3. Check if any connected "person" node matches the user
    # (sometimes the user's name exists as a separate Person node linked to the user node)
    if user_uuid:
        connected_person_uuids = set()
        for edge in graph_edges_raw:
            if edge.get("source_node") == user_uuid:
                connected_person_uuids.add(edge.get("target_node"))
            elif edge.get("target_node") == user_uuid:
                connected_person_uuids.add(edge.get("source_node"))
        
        for node in all_nodes:
            if (node.get("uuid") in connected_person_uuids 
                and node.get("type") == "person"
                and node.get("name")
                and "kwami_" not in (node.get("name") or "").lower()):
                # Check if the summary suggests this is the user themselves
                node_summary = (node.get("summary") or "").lower()
                if any(kw in node_summary for kw in ["identifies as", "the user", "self", "themselves"]):
                    return node["name"]
    
    # 4. Fall back to email-derived name
    if auth_user.email:
        return auth_user.email.split("@")[0].replace(".", " ").replace("_", " ").title()
    
    return "User"


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
    logger.info(f"📊 Fetching memory graph for user: {user_id} (limit={limit})")
    try:
        nodes = []
        edges = []
        graph_edges_raw = []  # Store raw edges for relationship building
        
        # 1. Get edges via graph API (for relationships)
        try:
            edges_response = await client.graph.edge.get_by_user_id(user_id=user_id, limit=limit)
            if edges_response:
                logger.info(f"📊 Got {len(edges_response)} edges from graph.edge")
                for edge in edges_response:
                    edge_data = {
                        "fact": getattr(edge, 'fact', None),
                        "relation": getattr(edge, 'name', 'related_to'),
                        "source_node": getattr(edge, 'source_node_uuid', None),
                        "target_node": getattr(edge, 'target_node_uuid', None),
                    }
                    graph_edges_raw.append(edge_data)
        except Exception as e:
            logger.warning(f"📊 graph.edge failed, trying search: {e}")
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
                            "fact": getattr(edge, 'fact', None),
                            "relation": getattr(edge, 'name', 'related_to'),
                            "source_node": getattr(edge, 'source_node_uuid', None),
                            "target_node": getattr(edge, 'target_node_uuid', None),
                        }
                        graph_edges_raw.append(edge_data)
            except Exception as search_e:
                logger.warning(f"📊 graph.search edges also failed: {search_e}")
        
        # 2. Get nodes (entities) from graph.node API
        entity_nodes = []
        user_node_uuid = None  # Track the user/kwami node
        
        try:
            nodes_response = await client.graph.node.get_by_user_id(user_id=user_id, limit=limit)
            if nodes_response:
                logger.info(f"📊 Got {len(nodes_response)} nodes from graph.node")
                for node in nodes_response:
                    node_name = getattr(node, 'name', 'Unknown')
                    node_labels = list(node.labels) if hasattr(node, 'labels') and node.labels else []
                    node_uuid = getattr(node, 'uuid_', None) or getattr(node, 'uuid', None)
                    created_at = node.created_at if hasattr(node, 'created_at') else None
                    node_summary = getattr(node, 'summary', None)
                    
                    # Infer type using labels + keyword analysis
                    node_type = _infer_node_type(node_name, node_summary, node_labels)
                    
                    # Detect the user/kwami node
                    if node_type == "user" or "kwami_" in node_name.lower():
                        user_node_uuid = node_uuid
                        node_type = "user"
                    
                    entity_nodes.append({
                        "name": node_name,
                        "type": node_type,
                        "summary": node_summary,
                        "uuid": node_uuid,
                        "created_at": created_at,
                        "labels": node_labels,
                    })
        except Exception as e:
            logger.warning(f"📊 graph.node failed: {e}")
        
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
            
            nodes.append({
                "id": node_id,
                "label": label,
                "type": entity["type"],
                "summary": entity["summary"],
                "uuid": entity["uuid"],
                "created_at": entity["created_at"],
                "labels": entity["labels"],
                "val": 25 if entity["type"] == "user" else 15
            })
        
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
                    edges.append({
                        "source": source_id,
                        "target": target_id,
                        "relation": relation
                    })
                    edges_added.add(edge_key)
                    connected_nodes.add(source_id)
                    connected_nodes.add(target_id)
        
        # 5. Connect orphan nodes to user node if we have one
        if user_node_id:
            for node in nodes:
                if node["id"] != user_node_id and node["id"] not in connected_nodes:
                    edges.append({
                        "source": user_node_id,
                        "target": node["id"],
                        "relation": "related_to"
                    })
        
        logger.info(f"📊 Final graph: {len(nodes)} nodes, {len(edges)} edges")
        return {"nodes": nodes, "edges": edges}
        
    except Exception as e:
        logger.error(f"Failed to fetch memory graph: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Fact Rating
# =============================================================================

@router.get("/{user_id}/fact-rating")
async def get_fact_rating(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Get the current fact rating instruction for a user's graph.
    
    Fact rating lets you configure how Zep rates the importance of facts,
    so you can filter out low-value memories for your use case.
    """
    verify_user_access(user, user_id)
    logger.info(f"⭐ Fetching fact rating for user: {user_id}")
    
    try:
        # User graphs are identified by user_id as graph_id in Zep
        # Try to get the graph info to read fact_rating_instruction
        try:
            # Search with min_fact_rating to see if rating is configured
            # There's no direct "get graph by user" - try listing
            graphs_response = await client.graph.list_all(page_size=100)
            user_graph = None
            if graphs_response and hasattr(graphs_response, 'graphs'):
                for g in (graphs_response.graphs or []):
                    gid = getattr(g, 'graph_id', None) or getattr(g, 'uuid', None)
                    if gid and user_id in str(gid):
                        user_graph = g
                        break
            
            if user_graph and hasattr(user_graph, 'fact_rating_instruction') and user_graph.fact_rating_instruction:
                fri = user_graph.fact_rating_instruction
                result = {
                    "configured": True,
                    "instruction": getattr(fri, 'instruction', None),
                    "examples": None,
                }
                if hasattr(fri, 'examples') and fri.examples:
                    result["examples"] = {
                        "high": getattr(fri.examples, 'high', None),
                        "medium": getattr(fri.examples, 'medium', None),
                        "low": getattr(fri.examples, 'low', None),
                    }
                return result
        except Exception as e:
            logger.warning(f"⭐ Could not read graph info: {e}")
        
        return {"configured": False, "instruction": None, "examples": None}
        
    except Exception as e:
        logger.error(f"Failed to get fact rating: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{user_id}/fact-rating")
async def set_fact_rating(
    user_id: str,
    body: FactRatingRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Set fact rating instructions for a user's graph.
    
    This configures how Zep rates the importance of extracted facts.
    You provide an instruction string and three examples (high/medium/low).
    """
    verify_user_access(user, user_id)
    logger.info(f"⭐ Setting fact rating for user: {user_id}")
    
    try:
        from zep_cloud import FactRatingInstruction, FactRatingExamples
        
        fri = FactRatingInstruction(
            instruction=body.instruction,
            examples=FactRatingExamples(
                high=body.examples.high,
                medium=body.examples.medium,
                low=body.examples.low,
            ),
        )
        
        # Update the user's graph with the fact rating instruction
        # User graphs use user_id as the graph_id
        await client.graph.update(
            graph_id=user_id,
            fact_rating_instruction=fri,
        )
        
        logger.info(f"⭐ Fact rating set for user: {user_id}")
        return {
            "success": True,
            "user_id": user_id,
            "instruction": body.instruction,
        }
        
    except Exception as e:
        logger.error(f"Failed to set fact rating: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Custom Instructions
# =============================================================================

@router.get("/{user_id}/instructions")
async def get_custom_instructions(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Get all custom instructions for a user's graph.
    
    Custom instructions guide how Zep extracts and organizes knowledge
    from conversations for this specific user.
    """
    verify_user_access(user, user_id)
    logger.info(f"📝 Fetching custom instructions for user: {user_id}")
    
    try:
        response = await client.graph.list_custom_instructions(user_id=user_id)
        
        instructions = []
        if response and hasattr(response, 'instructions') and response.instructions:
            for inst in response.instructions:
                instructions.append({
                    "name": getattr(inst, 'name', ''),
                    "text": getattr(inst, 'text', ''),
                })
        
        return {"instructions": instructions, "count": len(instructions)}
        
    except Exception as e:
        logger.error(f"Failed to get custom instructions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/instructions")
async def add_custom_instructions(
    user_id: str,
    body: CustomInstructionsBody,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Add custom instructions for a user's graph.
    
    Instructions guide how Zep processes and extracts knowledge.
    Each instruction has a unique name and a text body.
    """
    verify_user_access(user, user_id)
    logger.info(f"📝 Adding {len(body.instructions)} custom instructions for user: {user_id}")
    
    try:
        from zep_cloud import CustomInstruction
        
        zep_instructions = [
            CustomInstruction(name=inst.name, text=inst.text)
            for inst in body.instructions
        ]
        
        await client.graph.add_custom_instructions(
            instructions=zep_instructions,
            user_ids=[user_id],
        )
        
        logger.info(f"📝 Added {len(body.instructions)} instructions for user: {user_id}")
        return {
            "success": True,
            "added": len(body.instructions),
        }
        
    except Exception as e:
        logger.error(f"Failed to add custom instructions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{user_id}/instructions")
async def delete_custom_instructions(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    names: Optional[str] = Query(None, description="Comma-separated instruction names to delete. If empty, deletes all."),
):
    """Delete custom instructions for a user's graph.
    
    Pass instruction names as a comma-separated query param to delete specific ones,
    or omit to delete all instructions for the user.
    """
    verify_user_access(user, user_id)
    
    instruction_names = None
    if names:
        instruction_names = [n.strip() for n in names.split(",") if n.strip()]
    
    logger.info(f"📝 Deleting instructions for user: {user_id} (names={instruction_names})")
    
    try:
        await client.graph.delete_custom_instructions(
            user_ids=[user_id],
            instruction_names=instruction_names,
        )
        
        return {"success": True, "deleted": instruction_names or "all"}
        
    except Exception as e:
        logger.error(f"Failed to delete custom instructions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Data Re-ingestion
# =============================================================================

@router.post("/{user_id}/ingest")
async def ingest_data(
    user_id: str,
    body: IngestRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Ingest data into the user's knowledge graph.
    
    Feed text or JSON data through Zep's entity extraction pipeline
    to create new facts and entities. Useful for:
    - Adding context the AI should remember
    - Re-processing corrected information
    - Importing external knowledge
    
    Supported types: 'text', 'json', 'message'
    """
    verify_user_access(user, user_id)
    logger.info(f"📥 Ingesting data for user: {user_id} (type={body.type}, len={len(body.data)})")
    
    try:
        add_kwargs = {
            "data": body.data,
            "type": body.type,
            "user_id": user_id,
        }
        if body.source_description:
            add_kwargs["source_description"] = body.source_description
        
        episode = await client.graph.add(**add_kwargs)
        
        episode_uuid = None
        if episode:
            episode_uuid = getattr(episode, 'uuid_', None) or getattr(episode, 'uuid', None)
        
        logger.info(f"📥 Data ingested as episode: {episode_uuid}")
        return {
            "success": True,
            "episode_uuid": episode_uuid,
            "data_length": len(body.data),
            "type": body.type,
        }
        
    except Exception as e:
        logger.error(f"Failed to ingest data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Graph Analysis: Communities, Duplicates, Merge, Reorganize
# =============================================================================

async def _fetch_graph_raw(client: AsyncZep, user_id: str, limit: int = 200):
    """Helper: fetch raw nodes and edges from Zep for graph analysis."""
    nodes_list = []
    edges_list = []

    try:
        nodes_response = await client.graph.node.get_by_user_id(user_id=user_id, limit=limit)
        if nodes_response:
            for n in nodes_response:
                nodes_list.append({
                    "uuid": getattr(n, 'uuid_', None) or getattr(n, 'uuid', None),
                    "name": getattr(n, 'name', 'Unknown'),
                    "summary": getattr(n, 'summary', None),
                    "labels": list(n.labels) if hasattr(n, 'labels') and n.labels else [],
                })
    except Exception as e:
        logger.warning(f"Failed to fetch nodes for analysis: {e}")

    try:
        edges_response = await client.graph.edge.get_by_user_id(user_id=user_id, limit=limit)
        if edges_response:
            for e in edges_response:
                edges_list.append({
                    "uuid": getattr(e, 'uuid_', None) or getattr(e, 'uuid', None),
                    "source_node_uuid": getattr(e, 'source_node_uuid', None),
                    "target_node_uuid": getattr(e, 'target_node_uuid', None),
                    "fact": getattr(e, 'fact', None),
                    "name": getattr(e, 'name', None),
                    "valid_at": str(e.valid_at) if hasattr(e, 'valid_at') and e.valid_at else None,
                    "invalid_at": str(e.invalid_at) if hasattr(e, 'invalid_at') and e.invalid_at else None,
                })
    except Exception as e:
        logger.warning(f"Failed to fetch edges for analysis: {e}")

    return nodes_list, edges_list


@router.get("/{user_id}/communities")
async def detect_communities(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    resolution: float = Query(1.0, ge=0.1, le=5.0, description="Louvain resolution (higher = more communities)"),
):
    """Detect communities in the user's knowledge graph using the Louvain algorithm.

    Groups strongly connected nodes together and returns community assignments.
    """
    verify_user_access(user, user_id)
    logger.info(f"🔬 Detecting communities for user: {user_id} (resolution={resolution})")

    try:
        import networkx as nx
        from community import community_louvain

        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if not nodes_list:
            return {"communities": [], "count": 0}

        # Build networkx graph
        G = nx.Graph()
        uuid_to_name = {}
        for n in nodes_list:
            if n["uuid"]:
                G.add_node(n["uuid"])
                uuid_to_name[n["uuid"]] = n["name"]

        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src and tgt and G.has_node(src) and G.has_node(tgt):
                G.add_edge(src, tgt)

        # Run Louvain community detection
        if G.number_of_nodes() == 0:
            return {"communities": [], "count": 0}

        partition = community_louvain.best_partition(G, resolution=resolution)

        # Group nodes by community
        communities_map: dict[int, list[str]] = {}
        for node_uuid, comm_id in partition.items():
            communities_map.setdefault(comm_id, []).append(node_uuid)

        # Build response
        communities = []
        for comm_id, member_uuids in sorted(communities_map.items(), key=lambda x: -len(x[1])):
            members = []
            for uid in member_uuids:
                node_data = next((n for n in nodes_list if n["uuid"] == uid), None)
                if node_data:
                    members.append({
                        "uuid": uid,
                        "name": node_data["name"],
                        "summary": node_data["summary"],
                        "labels": node_data["labels"],
                    })

            # Generate a community label from member names
            member_names = [m["name"] for m in members[:5]]
            label = ", ".join(member_names)
            if len(members) > 5:
                label += f" (+{len(members) - 5} more)"

            communities.append({
                "id": comm_id,
                "label": label,
                "members": members,
                "size": len(members),
            })

        logger.info(f"🔬 Found {len(communities)} communities across {len(nodes_list)} nodes")
        return {"communities": communities, "count": len(communities)}

    except Exception as e:
        logger.error(f"Failed to detect communities: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/duplicates")
async def detect_duplicates(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    threshold: int = Query(80, ge=50, le=100, description="Fuzzy match threshold (0-100, higher = stricter)"),
):
    """Detect potential duplicate nodes using fuzzy string matching on names.

    Returns pairs of nodes that look like duplicates with similarity scores.
    """
    verify_user_access(user, user_id)
    logger.info(f"🔍 Detecting duplicates for user: {user_id} (threshold={threshold})")

    try:
        from thefuzz import fuzz

        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if len(nodes_list) < 2:
            return {"duplicates": [], "count": 0}

        # Count edges per node for "importance" ranking
        edge_count: dict[str, int] = {}
        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src:
                edge_count[src] = edge_count.get(src, 0) + 1
            if tgt:
                edge_count[tgt] = edge_count.get(tgt, 0) + 1

        # Compare all pairs
        duplicates = []
        seen = set()

        for i, a in enumerate(nodes_list):
            for b in nodes_list[i + 1:]:
                if not a["name"] or not b["name"]:
                    continue

                # Fuzzy match on name
                name_score = fuzz.ratio(a["name"].lower(), b["name"].lower())
                # Also check token sort ratio for reordered words
                token_score = fuzz.token_sort_ratio(a["name"].lower(), b["name"].lower())
                score = max(name_score, token_score)

                if score >= threshold:
                    pair_key = tuple(sorted([a["uuid"], b["uuid"]]))
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)

                    # Suggest which to keep (more edges = more important)
                    a_edges = edge_count.get(a["uuid"], 0)
                    b_edges = edge_count.get(b["uuid"], 0)
                    if a_edges >= b_edges:
                        keep, remove = a, b
                    else:
                        keep, remove = b, a

                    duplicates.append({
                        "score": score,
                        "keep": {
                            "uuid": keep["uuid"],
                            "name": keep["name"],
                            "summary": keep["summary"],
                            "labels": keep["labels"],
                            "edge_count": edge_count.get(keep["uuid"], 0),
                        },
                        "remove": {
                            "uuid": remove["uuid"],
                            "name": remove["name"],
                            "summary": remove["summary"],
                            "labels": remove["labels"],
                            "edge_count": edge_count.get(remove["uuid"], 0),
                        },
                    })

        # Sort by score descending
        duplicates.sort(key=lambda x: -x["score"])

        logger.info(f"🔍 Found {len(duplicates)} duplicate candidates")
        return {"duplicates": duplicates, "count": len(duplicates)}

    except Exception as e:
        logger.error(f"Failed to detect duplicates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/merge")
async def merge_nodes(
    user_id: str,
    body: MergeNodesRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Merge two duplicate nodes by re-pointing all edges to the kept node and deleting the duplicate.

    Steps:
    1. Fetch all edges of the node being removed
    2. Recreate each edge pointing to the kept node via add_fact_triple
    3. Delete the duplicate node (cascades its old edges)
    """
    verify_user_access(user, user_id)
    logger.info(f"🔗 Merging nodes for user: {user_id} (keep={body.keep_uuid}, remove={body.remove_uuid})")

    try:
        # 1. Fetch the kept node info
        try:
            keep_node = await client.graph.node.get(uuid_=body.keep_uuid)
        except Exception:
            raise HTTPException(status_code=404, detail=f"Keep node {body.keep_uuid} not found")

        keep_name = getattr(keep_node, 'name', 'Unknown')
        keep_summary = getattr(keep_node, 'summary', None)

        # 2. Fetch all edges connected to the node being removed
        try:
            remove_edges = await client.graph.node.get_edges(node_uuid=body.remove_uuid)
        except Exception:
            remove_edges = []

        # 3. Recreate edges pointing to the kept node
        recreated = 0
        for edge in (remove_edges or []):
            edge_source = getattr(edge, 'source_node_uuid', None)
            edge_target = getattr(edge, 'target_node_uuid', None)
            edge_fact = getattr(edge, 'fact', 'related')
            edge_name = getattr(edge, 'name', 'RELATED_TO')

            # Skip self-loops and edges between the two merging nodes
            if edge_source == body.keep_uuid or edge_target == body.keep_uuid:
                continue

            triple_kwargs = {
                "fact": edge_fact or "related",
                "fact_name": edge_name or "RELATED_TO",
                "user_id": user_id,
            }

            # Redirect the edge to point to the kept node
            if edge_source == body.remove_uuid:
                triple_kwargs["source_node_uuid"] = body.keep_uuid
                triple_kwargs["target_node_uuid"] = edge_target
            elif edge_target == body.remove_uuid:
                triple_kwargs["source_node_uuid"] = edge_source
                triple_kwargs["target_node_uuid"] = body.keep_uuid
            else:
                continue

            # Preserve temporal data
            if hasattr(edge, 'valid_at') and edge.valid_at:
                triple_kwargs["valid_at"] = str(edge.valid_at)
            if hasattr(edge, 'invalid_at') and edge.invalid_at:
                triple_kwargs["invalid_at"] = str(edge.invalid_at)

            try:
                await client.graph.add_fact_triple(**triple_kwargs)
                recreated += 1
            except Exception as te:
                logger.warning(f"🔗 Failed to recreate edge during merge: {te}")

        # 4. Delete the duplicate node (this also removes its old edges)
        try:
            await client.graph.node.delete(uuid_=body.remove_uuid)
        except Exception as de:
            logger.warning(f"🔗 Failed to delete merged node: {de}")

        logger.info(f"🔗 Merge complete: kept {body.keep_uuid}, removed {body.remove_uuid}, recreated {recreated} edges")
        return {
            "success": True,
            "keep_uuid": body.keep_uuid,
            "keep_name": keep_name,
            "removed_uuid": body.remove_uuid,
            "recreated_edges": recreated,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to merge nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/reorganize/preview")
async def reorganize_preview(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    duplicate_threshold: int = Query(95, ge=80, le=100),
):
    """Dry-run reorganization: returns orphan nodes and duplicate pairs
    without modifying anything. The user can review and select which
    actions to apply.
    """
    verify_user_access(user, user_id)
    logger.info(f"🔍 Reorganize preview for user: {user_id}")

    try:
        from thefuzz import fuzz

        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if not nodes_list:
            return {"orphans": [], "duplicates": [], "communities_estimate": 0}

        # --- Detect orphan nodes ---
        node_uuids_with_edges = set()
        for e in edges_list:
            if e["source_node_uuid"]:
                node_uuids_with_edges.add(e["source_node_uuid"])
            if e["target_node_uuid"]:
                node_uuids_with_edges.add(e["target_node_uuid"])

        orphans = []
        for n in nodes_list:
            nid = n["uuid"]
            name_lower = (n["name"] or "").lower()
            if not nid or nid in node_uuids_with_edges:
                continue
            if "kwami_" in name_lower or name_lower == "user":
                continue
            orphans.append({
                "uuid": nid,
                "name": n["name"],
                "summary": n["summary"],
                "labels": n["labels"],
            })

        # --- Detect duplicate pairs ---
        edge_count: dict[str, int] = {}
        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src:
                edge_count[src] = edge_count.get(src, 0) + 1
            if tgt:
                edge_count[tgt] = edge_count.get(tgt, 0) + 1

        duplicates = []
        seen = set()
        for i, a in enumerate(nodes_list):
            if not a["name"]:
                continue
            for b in nodes_list[i + 1:]:
                if not b["name"]:
                    continue
                score = max(
                    fuzz.ratio(a["name"].lower(), b["name"].lower()),
                    fuzz.token_sort_ratio(a["name"].lower(), b["name"].lower()),
                )
                if score >= duplicate_threshold:
                    pair_key = tuple(sorted([a["uuid"], b["uuid"]]))
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)

                    a_edges = edge_count.get(a["uuid"], 0)
                    b_edges = edge_count.get(b["uuid"], 0)
                    keep = a if a_edges >= b_edges else b
                    remove = b if keep == a else a

                    duplicates.append({
                        "score": score,
                        "keep": {
                            "uuid": keep["uuid"],
                            "name": keep["name"],
                            "summary": keep["summary"],
                            "edge_count": edge_count.get(keep["uuid"], 0),
                        },
                        "remove": {
                            "uuid": remove["uuid"],
                            "name": remove["name"],
                            "summary": remove["summary"],
                            "edge_count": edge_count.get(remove["uuid"], 0),
                        },
                    })

        duplicates.sort(key=lambda x: -x["score"])

        # --- Estimate communities ---
        import networkx as nx
        from community import community_louvain
        G = nx.Graph()
        for n in nodes_list:
            if n["uuid"]:
                G.add_node(n["uuid"])
        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src and tgt and G.has_node(src) and G.has_node(tgt):
                G.add_edge(src, tgt)
        communities_estimate = 0
        if G.number_of_nodes() > 0:
            partition = community_louvain.best_partition(G)
            communities_estimate = len(set(partition.values()))

        logger.info(
            f"🔍 Preview: {len(orphans)} orphans, {len(duplicates)} duplicates, "
            f"{communities_estimate} communities"
        )
        return {
            "orphans": orphans,
            "duplicates": duplicates,
            "communities_estimate": communities_estimate,
        }

    except Exception as e:
        logger.error(f"Failed reorganize preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/reorganize/apply")
async def reorganize_apply(
    user_id: str,
    body: ReorganizeApplyRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Apply selected reorganization actions: delete specific orphans
    and merge specific duplicate pairs.
    """
    verify_user_access(user, user_id)
    logger.info(
        f"🧹 Applying reorganization for user: {user_id} "
        f"({len(body.orphan_uuids)} orphans, {len(body.merge_pairs)} merges)"
    )

    try:
        report = {"orphans_removed": 0, "merges_performed": 0, "errors": []}

        # --- Remove selected orphans ---
        for orphan_uuid in body.orphan_uuids:
            try:
                await client.graph.node.delete(uuid_=orphan_uuid)
                report["orphans_removed"] += 1
            except Exception as de:
                report["errors"].append(f"Failed to remove orphan {orphan_uuid}: {str(de)[:80]}")

        # --- Merge selected pairs ---
        for pair in body.merge_pairs:
            try:
                # Fetch edges of the node being removed
                try:
                    remove_edges = await client.graph.node.get_edges(node_uuid=pair.remove_uuid)
                except Exception:
                    remove_edges = []

                # Re-point edges to the kept node
                for edge in (remove_edges or []):
                    es = getattr(edge, 'source_node_uuid', None)
                    et = getattr(edge, 'target_node_uuid', None)
                    if es == pair.keep_uuid or et == pair.keep_uuid:
                        continue

                    triple_kwargs = {
                        "fact": getattr(edge, 'fact', 'related') or "related",
                        "fact_name": getattr(edge, 'name', 'RELATED_TO') or "RELATED_TO",
                        "user_id": user_id,
                    }
                    if es == pair.remove_uuid:
                        triple_kwargs["source_node_uuid"] = pair.keep_uuid
                        triple_kwargs["target_node_uuid"] = et
                    elif et == pair.remove_uuid:
                        triple_kwargs["source_node_uuid"] = es
                        triple_kwargs["target_node_uuid"] = pair.keep_uuid
                    else:
                        continue

                    try:
                        await client.graph.add_fact_triple(**triple_kwargs)
                    except Exception as te:
                        report["errors"].append(f"Edge recreate failed: {str(te)[:80]}")

                # Delete the duplicate node
                await client.graph.node.delete(uuid_=pair.remove_uuid)
                report["merges_performed"] += 1
            except Exception as me:
                report["errors"].append(f"Merge failed for {pair.remove_uuid}: {str(me)[:80]}")

        logger.info(f"🧹 Apply complete: {report}")
        return {"success": True, "report": report}

    except Exception as e:
        logger.error(f"Failed to apply reorganization: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/reorganize")
async def reorganize_graph(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    auto_merge_threshold: int = Query(95, ge=80, le=100, description="Auto-merge similarity threshold"),
):
    """One-click graph reorganization: remove orphans, auto-merge high-confidence duplicates,
    and detect communities.

    Steps:
    1. Remove orphan nodes (0 edges, not the user node)
    2. Auto-merge nodes with very high name similarity (>= threshold)
    3. Run community detection on the cleaned graph
    """
    verify_user_access(user, user_id)
    logger.info(f"🧹 Reorganizing graph for user: {user_id} (threshold={auto_merge_threshold})")

    try:
        import networkx as nx
        from community import community_louvain
        from thefuzz import fuzz

        report = {
            "orphans_removed": 0,
            "merges_performed": 0,
            "communities_found": 0,
            "errors": [],
        }

        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if not nodes_list:
            return {"success": True, "report": report}

        # --- Step 1: Remove orphan nodes ---
        node_uuids_with_edges = set()
        for e in edges_list:
            if e["source_node_uuid"]:
                node_uuids_with_edges.add(e["source_node_uuid"])
            if e["target_node_uuid"]:
                node_uuids_with_edges.add(e["target_node_uuid"])

        for n in nodes_list:
            nid = n["uuid"]
            name_lower = (n["name"] or "").lower()
            # Skip the user node and nodes with connections
            if not nid or nid in node_uuids_with_edges:
                continue
            if "kwami_" in name_lower or name_lower == "user":
                continue

            try:
                await client.graph.node.delete(uuid_=nid)
                report["orphans_removed"] += 1
                logger.info(f"🧹 Removed orphan node: {n['name']} ({nid})")
            except Exception as de:
                report["errors"].append(f"Failed to remove orphan {nid}: {str(de)}")

        # Refresh data after orphan removal
        if report["orphans_removed"] > 0:
            nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        # --- Step 2: Auto-merge high-confidence duplicates ---
        merged_uuids = set()
        edge_count: dict[str, int] = {}
        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src:
                edge_count[src] = edge_count.get(src, 0) + 1
            if tgt:
                edge_count[tgt] = edge_count.get(tgt, 0) + 1

        for i, a in enumerate(nodes_list):
            if a["uuid"] in merged_uuids or not a["name"]:
                continue
            for b in nodes_list[i + 1:]:
                if b["uuid"] in merged_uuids or not b["name"]:
                    continue

                score = max(
                    fuzz.ratio(a["name"].lower(), b["name"].lower()),
                    fuzz.token_sort_ratio(a["name"].lower(), b["name"].lower()),
                )

                if score >= auto_merge_threshold:
                    # Decide which to keep
                    a_edges = edge_count.get(a["uuid"], 0)
                    b_edges = edge_count.get(b["uuid"], 0)
                    keep = a if a_edges >= b_edges else b
                    remove = b if keep == a else a

                    keep_node_obj = await client.graph.node.get(uuid_=keep["uuid"])
                    keep_name = getattr(keep_node_obj, 'name', keep["name"])

                    # Fetch and re-point edges
                    try:
                        remove_edges = await client.graph.node.get_edges(node_uuid=remove["uuid"])
                    except Exception:
                        remove_edges = []

                    for edge in (remove_edges or []):
                        es = getattr(edge, 'source_node_uuid', None)
                        et = getattr(edge, 'target_node_uuid', None)
                        if es == keep["uuid"] or et == keep["uuid"]:
                            continue

                        triple_kwargs = {
                            "fact": getattr(edge, 'fact', 'related') or "related",
                            "fact_name": getattr(edge, 'name', 'RELATED_TO') or "RELATED_TO",
                            "user_id": user_id,
                        }
                        if es == remove["uuid"]:
                            triple_kwargs["source_node_uuid"] = keep["uuid"]
                            triple_kwargs["target_node_uuid"] = et
                        elif et == remove["uuid"]:
                            triple_kwargs["source_node_uuid"] = es
                            triple_kwargs["target_node_uuid"] = keep["uuid"]
                        else:
                            continue

                        try:
                            await client.graph.add_fact_triple(**triple_kwargs)
                        except Exception as te:
                            report["errors"].append(f"Edge recreate failed: {str(te)[:80]}")

                    # Delete the duplicate
                    try:
                        await client.graph.node.delete(uuid_=remove["uuid"])
                        merged_uuids.add(remove["uuid"])
                        report["merges_performed"] += 1
                        logger.info(f"🧹 Auto-merged: '{remove['name']}' -> '{keep_name}'")
                    except Exception as de:
                        report["errors"].append(f"Delete failed for {remove['uuid']}: {str(de)[:80]}")

        # --- Step 3: Community detection on cleaned graph ---
        if report["orphans_removed"] > 0 or report["merges_performed"] > 0:
            nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        G = nx.Graph()
        for n in nodes_list:
            if n["uuid"]:
                G.add_node(n["uuid"])
        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src and tgt and G.has_node(src) and G.has_node(tgt):
                G.add_edge(src, tgt)

        if G.number_of_nodes() > 0:
            partition = community_louvain.best_partition(G)
            num_communities = len(set(partition.values()))
            report["communities_found"] = num_communities

        logger.info(f"🧹 Reorganization complete: {report}")
        return {"success": True, "report": report}

    except Exception as e:
        logger.error(f"Failed to reorganize graph: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Connect Nodes
# =============================================================================

@router.post("/{user_id}/connect")
async def connect_nodes(
    user_id: str,
    body: ConnectNodesRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Create a new edge (relationship) between two existing nodes.

    Uses add_fact_triple to create the connection in the knowledge graph.
    """
    verify_user_access(user, user_id)
    logger.info(
        f"🔗 Connecting nodes for user: {user_id} "
        f"({body.source_node_uuid} --[{body.relation}]--> {body.target_node_uuid})"
    )

    try:
        # Fetch node names -- add_fact_triple requires source/target node names
        try:
            source_node = await client.graph.node.get(uuid_=body.source_node_uuid)
            source_name = getattr(source_node, 'name', None)
        except Exception:
            raise HTTPException(status_code=404, detail=f"Source node {body.source_node_uuid} not found")

        try:
            target_node = await client.graph.node.get(uuid_=body.target_node_uuid)
            target_name = getattr(target_node, 'name', None)
        except Exception:
            raise HTTPException(status_code=404, detail=f"Target node {body.target_node_uuid} not found")

        fact_text = body.fact or f"{source_name} {body.relation.lower().replace('_', ' ')} {target_name}"

        triple_kwargs: dict = {
            "fact": fact_text,
            "fact_name": body.relation,
            "source_node_uuid": body.source_node_uuid,
            "source_node_name": source_name,
            "target_node_uuid": body.target_node_uuid,
            "target_node_name": target_name,
            "user_id": user_id,
        }

        result = await client.graph.add_fact_triple(**triple_kwargs)

        edge_uuid = None
        if result:
            edge_uuid = getattr(result, 'edge_uuid', None) or getattr(result, 'uuid_', None)

        logger.info(f"🔗 Edge created: {edge_uuid}")
        return {
            "success": True,
            "edge_uuid": edge_uuid,
            "source_node_uuid": body.source_node_uuid,
            "target_node_uuid": body.target_node_uuid,
            "relation": body.relation,
            "fact": fact_text,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to connect nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))
