"""Conversations as sessions: the list, one transcript, its summary, and status.

`GET /messages` flattens every session into one newest-first list, which is how
the panel came to show a single undifferentiated stream. These routes keep each
conversation whole: a session list with when it happened, how long it ran and
how it opened; the transcript of one session in order; and the summary Zep
writes for a thread as it goes.

`GET /status` answers the question an empty panel raises -- is memory switched
on, does Zep know this kwami, has it ever recorded a conversation.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from zep_cloud.client import AsyncZep

from src.api.deps import require_auth
from src.core.security import AuthUser
from src.services.memory import get_zep_client, verify_user_access
from src.services.memory.threads import (
    describe_threads,
    message_row,
    messages_of,
    owns_thread,
    user_threads,
)

logger = logging.getLogger("kwami-api.memory")
router = APIRouter()

UNAVAILABLE = "Memory service unavailable"


def _not_found(error: Exception) -> bool:
    return "404" in str(error) or type(error).__name__ == "NotFoundError"


async def _threads_or_502(client: AsyncZep, user_id: str) -> list:
    try:
        return await user_threads(client, user_id)
    except Exception as e:
        logger.warning("💬 user.get_threads failed: %s", e)
        raise HTTPException(status_code=502, detail=UNAVAILABLE) from e


@router.get("/{user_id}/sessions")
async def list_sessions(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, description="`next_offset` from the previous page"),
    include_empty: bool = Query(False, description="List sessions where nothing was said"),
):
    """The user's conversations, newest first, one entry per session.

    A session that opened and closed without a word (a reconnect, a dropped
    call) is left out unless `include_empty`. Pages are counted in threads, so
    `next_offset` continues after the last thread this page looked at.
    """
    verify_user_access(user, user_id)
    threads = await _threads_or_502(client, user_id)

    sessions: list[dict] = []
    index = offset
    while index < len(threads) and len(sessions) < limit:
        batch = threads[index : index + (limit - len(sessions))]
        index += len(batch)
        for described in await describe_threads(client, batch):
            if described["empty"] and not include_empty:
                continue
            sessions.append(described)

    return {
        "sessions": sessions,
        "count": len(sessions),
        "total_threads": len(threads),
        "next_offset": index if index < len(threads) else None,
    }


async def _owned_page(
    client: AsyncZep, user_id: str, thread_id: str, *, limit: int, page: int
) -> object:
    """One page of a thread, or a 404 if it is not the caller's.

    The owner comes back on the same answer, so the check costs nothing; the
    messages of a thread that is not the caller's are never returned.
    """
    try:
        response = await client.thread.get(thread_id=thread_id, limit=limit, cursor=page)
    except Exception as e:
        if _not_found(e):
            raise HTTPException(status_code=404, detail="Session not found") from e
        logger.warning("💬 thread.get failed for %s: %s", thread_id, e)
        raise HTTPException(status_code=502, detail=UNAVAILABLE) from e
    if not owns_thread(response, thread_id, user_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return response


@router.get("/{user_id}/sessions/{thread_id}/messages")
async def get_session_messages(
    user_id: str,
    thread_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
    limit: int = Query(200, ge=1, le=500),
    page: int = Query(1, ge=1, description="1-based page of `limit` messages"),
):
    """One session's transcript, in the order it was spoken."""
    verify_user_access(user, user_id)
    response = await _owned_page(client, user_id, thread_id, limit=limit, page=page)
    rows = [message_row(message, thread_id) for message in messages_of(response)]
    total = getattr(response, "total_count", None)
    if not isinstance(total, int):
        total = (page - 1) * limit + len(rows)
    return {
        "thread_id": thread_id,
        "messages": rows,
        "count": len(rows),
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": page * limit < total,
    }


@router.get("/{user_id}/sessions/{thread_id}/summary")
async def get_session_summary(
    user_id: str,
    thread_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """What Zep has summarised of one session so far; `summary` is null until
    there is enough to summarise."""
    verify_user_access(user, user_id)
    await _owned_page(client, user_id, thread_id, limit=1, page=1)
    try:
        summary = await client.thread.get_summary(thread_id)
    except Exception as e:
        if _not_found(e):
            return {"thread_id": thread_id, "summary": None, "updated_at": None}
        logger.warning("💬 thread.get_summary failed for %s: %s", thread_id, e)
        raise HTTPException(status_code=502, detail=UNAVAILABLE) from e
    updated = getattr(summary, "last_summarized_at", None) or getattr(summary, "created_at", None)
    return {
        "thread_id": thread_id,
        "summary": getattr(summary, "summary", None) or None,
        "updated_at": str(updated) if updated else None,
    }


@router.get("/{user_id}/status")
async def get_memory_status(
    user_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    client: AsyncZep = Depends(get_zep_client),
):
    """Whether this kwami has a memory yet, and when it last recorded a session.

    Reaching this route at all means the memory service is configured: without
    a Zep key the dependency answers 503 first.
    """
    verify_user_access(user, user_id)
    try:
        record = await client.user.get(user_id)
    except Exception as e:
        if not _not_found(e):
            logger.warning("💬 user.get failed: %s", e)
            raise HTTPException(status_code=502, detail=UNAVAILABLE) from e
        return {
            "configured": True,
            "user_exists": False,
            "created_at": None,
            "thread_count": 0,
            "last_session_at": None,
        }
    threads = await _threads_or_502(client, user_id)
    created = getattr(record, "created_at", None)
    last = getattr(threads[0], "created_at", None) if threads else None
    return {
        "configured": True,
        "user_exists": True,
        "created_at": str(created) if created else None,
        "thread_count": len(threads),
        "last_session_at": str(last) if last else None,
    }
