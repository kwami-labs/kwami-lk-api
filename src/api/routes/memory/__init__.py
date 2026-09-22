"""The memory HTTP surface.

This was one 2,900-line module holding 29 routes, 14 request models and the
graph algorithms. The algorithms moved to `src.services.memory`; the routes are
grouped here by what they operate on.

Sub-routers are included in the order their handlers were originally defined,
because FastAPI matches in registration order and several of these paths
overlap -- `GET /debug/{user_id}` and `GET /{user_id}/facts` both match
`/debug/facts`. `tests/unit/api/memory/test_route_table.py` pins the result.

The names below are re-exported because `src.api.routes.memory` is the import
path the application and the tests already use.
"""

from fastapi import APIRouter

from src.api.routes.memory import analysis, core, graph, ontology
from src.services.memory import (
    DEFAULT_EDGE_TYPES,
    DEFAULT_ENTITY_TYPES,
    close_zep_client,
    get_zep_client,
    iter_all_threads,
    thread_belongs_to,
    verify_user_access,
)

router = APIRouter()
router.include_router(core.router)
router.include_router(graph.router)
router.include_router(ontology.router)
router.include_router(analysis.router)

__all__ = [
    "DEFAULT_EDGE_TYPES",
    "DEFAULT_ENTITY_TYPES",
    "close_zep_client",
    "get_zep_client",
    "iter_all_threads",
    "router",
    "thread_belongs_to",
    "verify_user_access",
]
