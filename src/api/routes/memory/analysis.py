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
from src.core.security import AuthUser
from src.services.memory import (
    _fetch_graph_raw,
    get_zep_client,
    verify_user_access,
)

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()

# How many records are pulled from Zep before slicing a "page" in Python.
# Endpoints fetch this many regardless of the requested page size, so `total`
# saturates here and `has_more` is wrong beyond it.
ZEP_FETCH_LIMIT = 1000


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
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        raise HTTPException(status_code=500, detail=str(e)) from e


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
    """
    verify_user_access(user, user_id)
    logger.info("🔍 Detecting duplicates for user: %s (threshold=%s)", user_id, threshold)

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
            for b in nodes_list[i + 1 :]:
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

                    duplicates.append(
                        {
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
                        }
                    )

        # Sort by score descending
        duplicates.sort(key=lambda x: -x["score"])

        logger.info("🔍 Found %s duplicate candidates", len(duplicates))
        return {"duplicates": duplicates, "count": len(duplicates)}

    except Exception as e:
        logger.exception("Failed to detect duplicates")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
    logger.info(
        "🔗 Merging nodes for user: %s (keep=%s, remove=%s)",
        user_id,
        body.keep_uuid,
        body.remove_uuid,
    )

    try:
        # 1. Fetch the kept node info
        try:
            keep_node = await client.graph.node.get(uuid_=body.keep_uuid)
        except Exception:
            raise HTTPException(
                status_code=404, detail=f"Keep node {body.keep_uuid} not found"
            ) from None

        keep_name = getattr(keep_node, "name", "Unknown")

        # 2. Fetch all edges connected to the node being removed
        try:
            remove_edges = await client.graph.node.get_edges(node_uuid=body.remove_uuid)
        except Exception:
            remove_edges = []

        # 3. Recreate edges pointing to the kept node
        recreated = 0
        for edge in remove_edges or []:
            edge_source = getattr(edge, "source_node_uuid", None)
            edge_target = getattr(edge, "target_node_uuid", None)
            edge_fact = getattr(edge, "fact", "related")
            edge_name = getattr(edge, "name", "RELATED_TO")

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
            if hasattr(edge, "valid_at") and edge.valid_at:
                triple_kwargs["valid_at"] = str(edge.valid_at)
            if hasattr(edge, "invalid_at") and edge.invalid_at:
                triple_kwargs["invalid_at"] = str(edge.invalid_at)

            try:
                await client.graph.add_fact_triple(**triple_kwargs)
                recreated += 1
            except Exception as te:
                logger.warning("🔗 Failed to recreate edge during merge: %s", te)

        # 4. Delete the duplicate node (this also removes its old edges)
        try:
            await client.graph.node.delete(uuid_=body.remove_uuid)
        except Exception as de:
            logger.warning("🔗 Failed to delete merged node: %s", de)

        logger.info(
            "🔗 Merge complete: kept %s, removed %s, recreated %s edges",
            body.keep_uuid,
            body.remove_uuid,
            recreated,
        )
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
        logger.exception("Failed to merge nodes")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
            orphans.append(
                {
                    "uuid": nid,
                    "name": n["name"],
                    "summary": n["summary"],
                    "labels": n["labels"],
                }
            )

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
            for b in nodes_list[i + 1 :]:
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

                    duplicates.append(
                        {
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
                        }
                    )

        duplicates.sort(key=lambda x: -x["score"])

        # --- Estimate communities ---
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
        communities_estimate = 0
        if graph.number_of_nodes() > 0:
            partition = await anyio.to_thread.run_sync(
                partial(community_louvain.best_partition, graph)
            )
            communities_estimate = len(set(partition.values()))

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
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        "🧹 Applying reorganization for user: %s (%s orphans, %s merges)",
        user_id,
        len(body.orphan_uuids),
        len(body.merge_pairs),
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
                for edge in remove_edges or []:
                    es = getattr(edge, "source_node_uuid", None)
                    et = getattr(edge, "target_node_uuid", None)
                    if es == pair.keep_uuid or et == pair.keep_uuid:
                        continue

                    triple_kwargs = {
                        "fact": getattr(edge, "fact", "related") or "related",
                        "fact_name": getattr(edge, "name", "RELATED_TO") or "RELATED_TO",
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

        logger.info("🧹 Apply complete: %s", report)
        return {"success": True, "report": report}

    except Exception as e:
        logger.exception("Failed to apply reorganization")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
    1. Remove orphan nodes (0 edges, not the user node)
    2. Auto-merge nodes with very high name similarity (>= threshold)
    3. Run community detection on the cleaned graph
    """
    verify_user_access(user, user_id)
    logger.info("🧹 Reorganizing graph for user: %s (threshold=%s)", user_id, auto_merge_threshold)

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
                logger.info("🧹 Removed orphan node: %s (%s)", n["name"], nid)
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
            for b in nodes_list[i + 1 :]:
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
                    keep_name = getattr(keep_node_obj, "name", keep["name"])

                    # Fetch and re-point edges
                    try:
                        remove_edges = await client.graph.node.get_edges(node_uuid=remove["uuid"])
                    except Exception:
                        remove_edges = []

                    for edge in remove_edges or []:
                        es = getattr(edge, "source_node_uuid", None)
                        et = getattr(edge, "target_node_uuid", None)
                        if es == keep["uuid"] or et == keep["uuid"]:
                            continue

                        triple_kwargs = {
                            "fact": getattr(edge, "fact", "related") or "related",
                            "fact_name": getattr(edge, "name", "RELATED_TO") or "RELATED_TO",
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
                        logger.info("🧹 Auto-merged: '%s' -> '%s'", remove["name"], keep_name)
                    except Exception as de:
                        report["errors"].append(
                            f"Delete failed for {remove['uuid']}: {str(de)[:80]}"
                        )

        # --- Step 3: Community detection on cleaned graph ---
        if report["orphans_removed"] > 0 or report["merges_performed"] > 0:
            nodes_list, edges_list = await _fetch_graph_raw(client, user_id)

        graph = nx.Graph()
        for n in nodes_list:
            if n["uuid"]:
                graph.add_node(n["uuid"])
        for e in edges_list:
            src, tgt = e["source_node_uuid"], e["target_node_uuid"]
            if src and tgt and graph.has_node(src) and graph.has_node(tgt):
                graph.add_edge(src, tgt)

        if graph.number_of_nodes() > 0:
            partition = await anyio.to_thread.run_sync(
                partial(community_louvain.best_partition, graph)
            )
            num_communities = len(set(partition.values()))
            report["communities_found"] = num_communities

        logger.info("🧹 Reorganization complete: %s", report)
        return {"success": True, "report": report}

    except Exception as e:
        logger.exception("Failed to reorganize graph")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        raise HTTPException(status_code=500, detail=str(e)) from e
