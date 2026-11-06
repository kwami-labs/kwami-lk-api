"""Whether a node or edge uuid belongs to the graph of the user in the path.

`verify_user_access` proves the caller owns `/memory/{user_id}`. It says nothing
about the `edge_uuid` or `node_uuid` in the rest of the path or body, and Zep's
`graph.edge.delete(uuid_)` / `graph.node.delete(uuid_)` act on any uuid in the
project. So every route that edits, merges, connects or deletes by uuid let an
authenticated user act on another tenant's graph by naming one of its uuids.

Zep records carry no owner field, so ownership is asked of Zep through the
user-scoped listings: an edge is the caller's when the caller's edge listing,
filtered to that uuid, returns it; a node is the caller's when one of the
caller's edges touches it or, for a node with no edges, when it appears in the
caller's node listing. Both checks compare the uuids that come back, so a filter
Zep ignored fails closed instead of granting access.

A uuid that is not the caller's answers 404, the same as one that does not
exist, so the check is not an oracle for other tenants' uuids.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException
from zep_cloud import SearchFilters
from zep_cloud.client import AsyncZep

from src.services.memory.paging import uuid_of, walk


def _ends(edge: object) -> tuple[str | None, str | None]:
    return getattr(edge, "source_node_uuid", None), getattr(edge, "target_node_uuid", None)


async def edge_in_graph(client: AsyncZep, user_id: str, edge_uuid: str) -> bool:
    found = await client.graph.edge.get_by_user_id(
        user_id=user_id, limit=1, filters=SearchFilters(edge_uuids=[edge_uuid])
    )
    return any(uuid_of(edge) == edge_uuid for edge in found or [])


async def nodes_in_graph(client: AsyncZep, user_id: str, node_uuids: Iterable[str]) -> set[str]:
    """Which of `node_uuids` are in the user's graph, in two or three calls.

    One filtered edge listing answers for every node that has an edge. The
    rest can only be nodes with no edges, which least-connected-first ordering
    puts at the front of the node listing, so that walk stops at the first node
    that has one.
    """
    wanted = {uuid for uuid in node_uuids if uuid}
    found: set[str] = set()
    if not wanted:
        return found
    async for edge in walk(
        client.graph.edge.get_by_user_id,
        user_id,
        filters=SearchFilters(connected_node_uuids=sorted(wanted)),
    ):
        found.update(end for end in _ends(edge) if end in wanted)
        if found == wanted:
            return found
    missing = wanted - found
    async for node in walk(
        client.graph.node.get_by_user_id, user_id, order_by="degree", direction="asc"
    ):
        uuid = uuid_of(node)
        if uuid in missing:
            found.add(uuid)
            missing.discard(uuid)
        if not missing or (getattr(node, "degree", None) or 0) > 0:
            break
    return found


async def require_edge(client: AsyncZep, user_id: str, edge_uuid: str) -> None:
    if not await edge_in_graph(client, user_id, edge_uuid):
        raise HTTPException(status_code=404, detail=f"Edge {edge_uuid} not found")


async def require_nodes(client: AsyncZep, user_id: str, node_uuids: Iterable[str]) -> None:
    wanted = {uuid for uuid in node_uuids if uuid}
    missing = wanted - await nodes_in_graph(client, user_id, wanted)
    if missing:
        raise HTTPException(status_code=404, detail=f"Node {sorted(missing)[0]} not found")


async def require_node(client: AsyncZep, user_id: str, node_uuid: str) -> None:
    await require_nodes(client, user_id, [node_uuid])
