"""Memory domain logic, extracted from the route module.

`src/api/routes/memory.py` was 2,900 lines: 29 routes, 14 request models and the
graph algorithms in one file, and the only surface in the service with no
service layer beneath it. The routes now do HTTP; this package does the work.

Re-exported here so a caller writes `from src.services.memory import ...`
without caring which module a helper lives in.
"""

from src.services.memory.client import close_zep_client, get_zep_client
from src.services.memory.graph import (
    _extract_user_display_name,
    _fetch_graph_raw,
    _infer_node_type,
)
from src.services.memory.ontology import (
    DEFAULT_EDGE_TYPES,
    DEFAULT_ENTITY_TYPES,
    _build_ontology_models,
)
from src.services.memory.tenancy import (
    iter_all_threads,
    thread_belongs_to,
    verify_user_access,
)

__all__ = [
    "DEFAULT_EDGE_TYPES",
    "DEFAULT_ENTITY_TYPES",
    "_build_ontology_models",
    "_extract_user_display_name",
    "_fetch_graph_raw",
    "_infer_node_type",
    "close_zep_client",
    "get_zep_client",
    "iter_all_threads",
    "thread_belongs_to",
    "verify_user_access",
]
