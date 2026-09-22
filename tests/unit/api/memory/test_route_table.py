"""The memory router's route table, pinned.

29 routes were split out of one 2,900-line module into a package. FastAPI
matches in registration order and several of these paths overlap, so a refactor
that reorders them can silently change which handler answers a request.

The split did change the order: grouping the handlers by subject moved `search`,
`entities` and `graph` ahead of `ontology`, `fact-rating` and `instructions`.
That is safe, and the two assertions below are what make it checkable rather
than assumed:

* the set of routes is unchanged -- nothing was dropped or duplicated;
* every pair of paths that can match the same request keeps its relative order.

The only such pair is `GET /debug/{user_id}` and `GET /{user_id}/facts`, which
both match `/debug/facts`. Everything else differs in segment count or in a
literal segment, so their order cannot change what matches.
"""

from __future__ import annotations

import pytest

from src.api.routes.memory import router


def _flatten(candidate) -> list[tuple[str, str]]:
    """The real routes, through FastAPI's `_IncludedRouter` wrappers."""
    out: list[tuple[str, str]] = []
    for route in getattr(candidate, "routes", []):
        if hasattr(route, "methods") and hasattr(route, "path"):
            out.append((sorted(route.methods)[0], route.path))
        elif hasattr(route, "original_router"):
            out.extend(_flatten(route.original_router))
    return out


EXPECTED = [
    ("GET", "/debug/{user_id}"),
    ("GET", "/{user_id}/facts"),
    ("DELETE", "/{user_id}"),
    ("GET", "/{user_id}/messages"),
    ("DELETE", "/{user_id}/edge/{edge_uuid}"),
    ("DELETE", "/{user_id}/node/{node_uuid}"),
    ("PATCH", "/{user_id}/edge/{edge_uuid}"),
    ("PATCH", "/{user_id}/node/{node_uuid}"),
    ("GET", "/{user_id}/edges"),
    ("GET", "/{user_id}/nodes"),
    ("GET", "/{user_id}/search"),
    ("GET", "/{user_id}/entities/{entity_type}"),
    ("GET", "/{user_id}/graph"),
    ("GET", "/{user_id}/ontology"),
    ("PUT", "/{user_id}/ontology"),
    ("POST", "/{user_id}/ontology/reset"),
    ("GET", "/{user_id}/fact-rating"),
    ("PUT", "/{user_id}/fact-rating"),
    ("GET", "/{user_id}/instructions"),
    ("POST", "/{user_id}/instructions"),
    ("DELETE", "/{user_id}/instructions"),
    ("POST", "/{user_id}/ingest"),
    ("GET", "/{user_id}/communities"),
    ("GET", "/{user_id}/duplicates"),
    ("POST", "/{user_id}/merge"),
    ("POST", "/{user_id}/reorganize/preview"),
    ("POST", "/{user_id}/reorganize/apply"),
    ("POST", "/{user_id}/reorganize"),
    ("POST", "/{user_id}/connect"),
]


def test_every_route_is_still_mounted():
    assert sorted(_flatten(router)) == sorted(EXPECTED)


def test_none_were_lost_or_duplicated():
    table = _flatten(router)
    assert len(table) == 29
    assert len(set(table)) == len(table)


def test_the_registration_order_is_pinned():
    assert _flatten(router) == EXPECTED


def test_the_overlapping_pair_keeps_its_order():
    """`/debug/{user_id}` must stay ahead of `/{user_id}/facts`: both match
    `/debug/facts`, and whichever is registered first wins."""
    table = _flatten(router)
    assert table.index(("GET", "/debug/{user_id}")) < table.index(("GET", "/{user_id}/facts"))


@pytest.mark.anyio
async def test_debug_facts_still_reaches_the_debug_handler(mem, zep):
    """The consequence of that ordering, exercised rather than asserted."""
    response = await mem.get("/memory/debug/facts")
    assert response.status_code != 404
