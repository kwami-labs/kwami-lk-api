"""Who may read or destroy which memory.

Both helpers here replaced substring matching that granted cross-tenant access,
and `iter_all_threads` replaced a single-page read on the delete path. The
comments are the record of those fixes; they are load-bearing.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from zep_cloud.client import AsyncZep

from src.core.security import AuthUser, check_user_access

logger = logging.getLogger("kwami-api.memory")


def verify_user_access(user: AuthUser, user_id: str):
    """Verify the authenticated user has access to the requested user_id."""
    if not check_user_access(user, user_id):
        raise HTTPException(
            status_code=403, detail="Access denied: You can only access your own memory data"
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
