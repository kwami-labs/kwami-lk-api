"""The algorithms that inspect and rewrite the graph.\n\nCommunity detection, duplicate finding, merges and the reorganisation passes.\nThe CPU-bound parts are pushed to a worker thread: they stall the event loop\nexactly as a blocking socket read would.\n"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Query
from zep_cloud.client import AsyncZep

from src.api.deps import require_auth
from src.api.routes.memory.schemas import (
    ConnectNodesRequest,
    IngestRequest,
    MergeNodesRequest,
    ReorganizeApplyRequest,
)
from src.core.errors import failure_kind
from src.core.security import AuthUser
from src.services.memory import (
    _fetch_graph_raw,
    find_orphans,
    get_zep_client,
    has_edges,
    require_nodes,
    verify_user_access,
)
from src.services.memory import merge as merge_services
from src.services.memory.analysis import find_duplicate_pairs

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()


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
    logger.info(
        "📥 Ingesting data for user: %s (type=%s, len=%s)", user_id, body.type, len(body.data)
    )

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
            episode_uuid = getattr(episode, "uuid_", None) or getattr(episode, "uuid", None)

        logger.info("📥 Data ingested as episode: %s", episode_uuid)
        return {
            "success": True,
            "episode_uuid": episode_uuid,
            "data_length": len(body.data),
            "type": body.type,
        }

    except Exception as e:
        logger.exception("Failed to ingest data")
        raise HTTPException(status_code=500, detail="Failed to ingest data") from e


@router.get("/{user_id}/communities")
async def detect_communities(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    resolution: float = Query(
        1.0, ge=0.1, le=5.0, description="Louvain resolution (higher = more communities)"
    ),
):
    """Detect communities in the user's knowledge graph using the Louvain algorithm.

    Groups strongly connected nodes together and returns community assignments.
    """
    verify_user_access(user, user_id)
    logger.info("🔬 Detecting communities for user: %s (resolution=%s)", user_id, resolution)

    try:
        import networkx as nx
        from community import community_louvain

        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if not nodes_list:
            return {"communities": [], "count": 0}

        # Build networkx graph
        graph = nx.Graph()
        uuid_to_name = {}
        for n in nodes_list:
            if n["uuid"]:
                graph.add_node(n["uuid"])
                uuid_to_name[n["uuid"]] = n["name"]

        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src and tgt and graph.has_node(src) and graph.has_node(tgt):
                graph.add_edge(src, tgt)

        # Run Louvain community detection
        if graph.number_of_nodes() == 0:
            return {"communities": [], "count": 0}

        # Louvain is pure CPU and superlinear in the graph; on a large memory it
        # holds the event loop for as long as it runs, which stalls every other
        # request on the worker exactly as a blocking socket read would.
        partition = await anyio.to_thread.run_sync(
            partial(community_louvain.best_partition, graph, resolution=resolution)
        )

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
                    members.append(
                        {
                            "uuid": uid,
                            "name": node_data["name"],
                            "summary": node_data["summary"],
                            "labels": node_data["labels"],
                        }
                    )

            # Generate a community label from member names
            member_names = [m["name"] for m in members[:5]]
            label = ", ".join(member_names)
            if len(members) > 5:
                label += f" (+{len(members) - 5} more)"

            communities.append(
                {
                    "id": comm_id,
                    "label": label,
                    "members": members,
                    "size": len(members),
                }
            )

        logger.info("🔬 Found %s communities across %s nodes", len(communities), len(nodes_list))
        return {"communities": communities, "count": len(communities)}

    except Exception as e:
        logger.exception("Failed to detect communities")
        raise HTTPException(status_code=500, detail="Failed to detect communities") from e


@router.get("/{user_id}/duplicates")
async def detect_duplicates(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    threshold: int = Query(
        80, ge=50, le=100, description="Fuzzy match threshold (0-100, higher = stricter)"
    ),
):
    """Detect potential duplicate nodes using fuzzy string matching on names.

    Returns pairs of nodes that look like duplicates with similarity scores.
    Names are compared without case, accents or extra spacing.
    """
    verify_user_access(user, user_id)
    logger.info("🔍 Detecting duplicates for user: %s (threshold=%s)", user_id, threshold)

    try:
        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if len(nodes_list) < 2:
            return {"duplicates": [], "count": 0}

        duplicates = await anyio.to_thread.run_sync(
            partial(find_duplicate_pairs, nodes_list, edges_list, threshold)
        )

        logger.info("🔍 Found %s duplicate candidates", len(duplicates))
        return {"duplicates": duplicates, "count": len(duplicates)}

    except Exception as e:
        logger.exception("Failed to detect duplicates")
        raise HTTPException(status_code=500, detail="Failed to detect duplicates") from e


@router.post("/{user_id}/merge")
async def merge_nodes(
    user_id: str,
    body: MergeNodesRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Merge two duplicate nodes: move every fact of the duplicate onto the kept
    node, then delete the duplicate.

    The duplicate is deleted only once every fact has moved; otherwise it stays,
    with its facts, and the route answers 502 with what went wrong.
    """
    verify_user_access(user, user_id)
    await require_nodes(client, user_id, [body.keep_uuid, body.remove_uuid])
    logger.info(
        "🔗 Merging nodes for user: %s (keep=%s, remove=%s)",
        user_id,
        body.keep_uuid,
        body.remove_uuid,
    )

    try:
        try:
            keep_node = await client.graph.node.get(uuid_=body.keep_uuid)
        except Exception:
            raise HTTPException(
                status_code=404, detail=f"Keep node {body.keep_uuid} not found"
            ) from None

        keep_name = getattr(keep_node, "name", "Unknown")
        outcome = await merge_services.merge_nodes(
            client, user_id, body.keep_uuid, body.remove_uuid
        )
        if not outcome.removed:
            logger.warning("🔗 Merge left %s in place: %s", body.remove_uuid, outcome.errors)
            # A string detail, because that is what the app shows the user.
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Merge stopped after moving {outcome.moved} facts, so nothing was "
                    f"deleted: {outcome.errors[0]}"
                ),
            )

        logger.info(
            "🔗 Merge complete: kept %s, removed %s, moved %s edges",
            body.keep_uuid,
            body.remove_uuid,
            outcome.moved,
        )
        return {
            "success": True,
            "keep_uuid": body.keep_uuid,
            "keep_name": keep_name,
            "removed_uuid": body.remove_uuid,
            "recreated_edges": outcome.moved,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to merge nodes")
        raise HTTPException(status_code=500, detail="Failed to merge nodes") from e


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
    logger.info("🔍 Reorganize preview for user: %s", user_id)

    try:
        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if not nodes_list:
            return {"orphans": [], "duplicates": [], "communities_estimate": 0}

        orphans = [
            {
                "uuid": n["uuid"],
                "name": n["name"],
                "summary": n["summary"],
                "labels": n["labels"],
            }
            for n in find_orphans(nodes_list, edges_list)
        ]
        duplicates = await anyio.to_thread.run_sync(
            partial(find_duplicate_pairs, nodes_list, edges_list, duplicate_threshold)
        )
        communities_estimate = await _count_communities(nodes_list, edges_list)

        logger.info(
            "🔍 Preview: %s orphans, %s duplicates, %s communities",
            len(orphans),
            len(duplicates),
            communities_estimate,
        )
        return {
            "orphans": orphans,
            "duplicates": duplicates,
            "communities_estimate": communities_estimate,
        }

    except Exception as e:
        logger.exception("Failed reorganize preview")
        raise HTTPException(status_code=500, detail="Failed reorganize preview") from e


async def _count_communities(nodes_list: list[dict], edges_list: list[dict]) -> int:
    import networkx as nx
    from community import community_louvain

    graph = nx.Graph()
    for n in nodes_list:
        if n["uuid"]:
            graph.add_node(n["uuid"])
    for e in edges_list:
        src, tgt = e["source_node_uuid"], e["target_node_uuid"]
        if src and tgt and graph.has_node(src) and graph.has_node(tgt):
            graph.add_edge(src, tgt)
    if graph.number_of_nodes() == 0:
        return 0
    partition = await anyio.to_thread.run_sync(partial(community_louvain.best_partition, graph))
    return len(set(partition.values()))


async def _remove_orphan(client: AsyncZep, user_id: str, node_uuid: str, report: dict) -> None:
    """Delete a node the caller believes has no edges -- after asking Zep again.

    A preview can be minutes old, and a conversation in between can have given
    the node a fact; deleting it then would take that fact with it.
    """
    try:
        if await has_edges(client, user_id, node_uuid):
            report["orphans_kept"] += 1
            return
        await client.graph.node.delete(uuid_=node_uuid)
        report["orphans_removed"] += 1
    except Exception as de:
        logger.warning("Failed to remove an orphan: %s", de)
        report["errors"].append(f"Failed to remove orphan {node_uuid}: {failure_kind(de)}")


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
    # This route deletes every uuid it is handed, so all of them must be the
    # caller's -- checked against one listing, not one probe per uuid.
    await require_nodes(
        client,
        user_id,
        [
            *body.orphan_uuids,
            *(pair.keep_uuid for pair in body.merge_pairs),
            *(pair.remove_uuid for pair in body.merge_pairs),
        ],
    )
    logger.info(
        "🧹 Applying reorganization for user: %s (%s orphans, %s merges)",
        user_id,
        len(body.orphan_uuids),
        len(body.merge_pairs),
    )

    try:
        report = {"orphans_removed": 0, "orphans_kept": 0, "merges_performed": 0, "errors": []}

        for orphan_uuid in body.orphan_uuids:
            await _remove_orphan(client, user_id, orphan_uuid, report)

        for pair in body.merge_pairs:
            outcome = await merge_services.merge_nodes(
                client, user_id, pair.keep_uuid, pair.remove_uuid
            )
            report["errors"].extend(outcome.errors)
            if outcome.removed:
                report["merges_performed"] += 1

        logger.info("🧹 Apply complete: %s", report)
        return {"success": True, "report": report}

    except Exception as e:
        logger.exception("Failed to apply reorganization")
        raise HTTPException(status_code=500, detail="Failed to apply reorganization") from e


@router.post("/{user_id}/reorganize")
async def reorganize_graph(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    auto_merge_threshold: int = Query(
        95, ge=80, le=100, description="Auto-merge similarity threshold"
    ),
):
    """One-click graph reorganization: remove orphans, auto-merge high-confidence duplicates,
    and detect communities.

    Steps:
    1. Remove orphan nodes (no edges, not the user node), each re-checked with Zep first
    2. Auto-merge nodes with very high name similarity (>= threshold) whose types agree
    3. Run community detection on the cleaned graph
    """
    verify_user_access(user, user_id)
    logger.info("🧹 Reorganizing graph for user: %s (threshold=%s)", user_id, auto_merge_threshold)

    try:
        report = {
            "orphans_removed": 0,
            "orphans_kept": 0,
            "merges_performed": 0,
            "communities_found": 0,
            "errors": [],
        }

        nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        if not nodes_list:
            return {"success": True, "report": report}

        # --- Step 1: Remove orphan nodes ---
        for n in find_orphans(nodes_list, edges_list):
            await _remove_orphan(client, user_id, n["uuid"], report)

        # Refresh data after orphan removal
        if report["orphans_removed"] > 0:
            nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        # --- Step 2: Auto-merge high-confidence duplicates ---
        pairs = await anyio.to_thread.run_sync(
            partial(find_duplicate_pairs, nodes_list, edges_list, auto_merge_threshold)
        )
        touched: set[str] = set()
        for pair in pairs:
            keep, remove = pair["keep"]["uuid"], pair["remove"]["uuid"]
            # A node already merged away, or already absorbing another, is left
            # for the next pass: the pair was scored against the graph as it was.
            if not pair["compatible"] or keep in touched or remove in touched:
                continue
            outcome = await merge_services.merge_nodes(client, user_id, keep, remove)
            report["errors"].extend(outcome.errors)
            touched.update((keep, remove))
            if outcome.removed:
                report["merges_performed"] += 1
                logger.info("🧹 Auto-merged: '%s' -> '%s'", pair["remove"]["name"], keep)

        # --- Step 3: Community detection on cleaned graph ---
        if report["orphans_removed"] > 0 or report["merges_performed"] > 0:
            nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        report["communities_found"] = await _count_communities(nodes_list, edges_list)

        logger.info("🧹 Reorganization complete: %s", report)
        return {"success": True, "report": report}

    except Exception as e:
        logger.exception("Failed to reorganize graph")
        raise HTTPException(status_code=500, detail="Failed to reorganize graph") from e


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
    await require_nodes(client, user_id, [body.source_node_uuid, body.target_node_uuid])
    logger.info(
        "🔗 Connecting nodes for user: %s (%s --[%s]--> %s)",
        user_id,
        body.source_node_uuid,
        body.relation,
        body.target_node_uuid,
    )

    try:
        # Fetch node names -- add_fact_triple requires source/target node names
        try:
            source_node = await client.graph.node.get(uuid_=body.source_node_uuid)
            source_name = getattr(source_node, "name", None)
        except Exception:
            raise HTTPException(
                status_code=404, detail=f"Source node {body.source_node_uuid} not found"
            ) from None

        try:
            target_node = await client.graph.node.get(uuid_=body.target_node_uuid)
            target_name = getattr(target_node, "name", None)
        except Exception:
            raise HTTPException(
                status_code=404, detail=f"Target node {body.target_node_uuid} not found"
            ) from None

        fact_text = (
            body.fact or f"{source_name} {body.relation.lower().replace('_', ' ')} {target_name}"
        )

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
            edge_uuid = getattr(result, "edge_uuid", None) or getattr(result, "uuid_", None)

        logger.info("🔗 Edge created: %s", edge_uuid)
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
        logger.exception("Failed to connect nodes")
        raise HTTPException(status_code=500, detail="Failed to connect nodes") from e
