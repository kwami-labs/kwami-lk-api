"""Folding one entity into another without losing its facts.

The merge route, the reorganizer's apply step and its one-click pass each had
their own copy of this loop, and all three shared two faults:

- They asked `graph.node.get_edges` for the duplicate's facts. Measured against
  a live graph it under-reports badly (0 of 9, 1 of 5), so most facts were never
  moved -- and then the duplicate was deleted, which cascades to every fact it
  still held.
- They deleted the duplicate even when moving a fact had failed, taking that
  fact with it.

Here the facts come from the user-scoped edge listing filtered by the node,
which returned all of them, and the duplicate is deleted only once every fact
has been moved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from zep_cloud.client import AsyncZep

from src.core.errors import failure_kind
from src.services.memory.ownership import edges_of_node

logger = logging.getLogger("kwami-api.memory")

#: Edge timestamps carried over to the re-pointed fact.
_TIMESTAMPS = ("valid_at", "invalid_at", "expired_at")


@dataclass
class MergeOutcome:
    keep_uuid: str
    remove_uuid: str
    moved: int = 0
    removed: bool = False
    errors: list[str] = field(default_factory=list)


def _repointed(edge: object, keep_uuid: str, remove_uuid: str, user_id: str) -> dict | None:
    """The `add_fact_triple` arguments that move `edge` onto the kept node.

    None for an edge that would become a self-loop: one between the two nodes
    being merged, or one from the duplicate to itself.
    """
    source = getattr(edge, "source_node_uuid", None)
    target = getattr(edge, "target_node_uuid", None)
    if keep_uuid in (source, target) or source == target:
        return None
    kwargs: dict = {
        "fact": getattr(edge, "fact", None) or "related",
        "fact_name": getattr(edge, "name", None) or "RELATED_TO",
        "user_id": user_id,
        "source_node_uuid": keep_uuid if source == remove_uuid else source,
        "target_node_uuid": keep_uuid if target == remove_uuid else target,
    }
    for name in _TIMESTAMPS:
        value = getattr(edge, name, None)
        if value:
            kwargs[name] = str(value)
    attributes = getattr(edge, "attributes", None)
    if attributes:
        kwargs["edge_attributes"] = dict(attributes)
    return kwargs


async def merge_nodes(
    client: AsyncZep, user_id: str, keep_uuid: str, remove_uuid: str
) -> MergeOutcome:
    """Move every fact of `remove_uuid` onto `keep_uuid`, then delete it.

    Nothing is deleted unless every fact moved: a failed lookup or a failed move
    leaves the duplicate in place, with its facts, and says why.
    """
    outcome = MergeOutcome(keep_uuid=keep_uuid, remove_uuid=remove_uuid)
    try:
        edges = await edges_of_node(client, user_id, remove_uuid)
    except Exception as e:
        logger.warning("Could not list the facts of a duplicate: %s", e)
        outcome.errors.append(f"Could not list the facts of {remove_uuid}: {failure_kind(e)}")
        return outcome

    for edge in edges:
        kwargs = _repointed(edge, keep_uuid, remove_uuid, user_id)
        if kwargs is None:
            continue
        try:
            await client.graph.add_fact_triple(**kwargs)
            outcome.moved += 1
        except Exception as e:
            logger.warning("Edge recreate failed: %s", e)
            outcome.errors.append(f"Edge recreate failed: {failure_kind(e)}")

    if outcome.errors:
        return outcome
    try:
        await client.graph.node.delete(uuid_=remove_uuid)
        outcome.removed = True
    except Exception as e:
        logger.warning("Delete of a duplicate failed: %s", e)
        outcome.errors.append(f"Delete failed for {remove_uuid}: {failure_kind(e)}")
    return outcome
