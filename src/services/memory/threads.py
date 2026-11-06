"""A user's conversations: one Zep thread per session.

Every read of conversation history used to enumerate *every thread in the Zep
project* and keep the ones whose owner matched -- one walk over all tenants'
threads per request, which grows with the user base, not with the caller.
`user.get_threads` lists one user's threads directly. The anchored ownership
check still runs on what comes back, as a second line of defence.

Measured against the live project on 2026-11-06, `thread.get` behaves like this:
`limit=n` returns the first n messages, oldest first, with `total_count` set;
`lastn=n` returns the last n (and no total); `cursor` is a 1-based page number
of size `limit`. `user.get_threads` returns oldest first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from zep_cloud.client import AsyncZep

from src.services.memory.tenancy import thread_belongs_to

logger = logging.getLogger("kwami-api.memory")

#: Zep calls in flight at once when describing many sessions.
SESSION_CONCURRENCY = 8
#: Messages read from the start of a session to find its opening line.
PREVIEW_MESSAGES = 4
PREVIEW_CHARS = 160


def thread_id_of(thread: Any) -> str | None:
    return getattr(thread, "thread_id", None) or getattr(thread, "uuid_", None)


def messages_of(response: Any) -> list[Any]:
    """The messages of a `thread.get` answer, whichever shape Zep used."""
    if response is None:
        return []
    if isinstance(response, list):
        return response
    return list(getattr(response, "messages", None) or [])


def _when(record: Any, name: str = "created_at") -> str | None:
    value = getattr(record, name, None)
    return str(value) if value else None


def role_of(message: Any) -> str | None:
    role = getattr(message, "role", None) or getattr(message, "role_type", None)
    return str(getattr(role, "value", role)) if role else None


def message_row(message: Any, thread_id: str | None) -> dict:
    return {
        "uuid": getattr(message, "uuid_", None) or getattr(message, "uuid", None),
        "content": getattr(message, "content", None),
        "role": role_of(message),
        "role_type": getattr(message, "role_type", None),
        "name": getattr(message, "name", None),
        "created_at": _when(message),
        "thread_id": thread_id,
    }


async def user_threads(client: AsyncZep, user_id: str) -> list[Any]:
    """The user's threads, newest first. A user Zep has never seen has none."""
    try:
        threads = await client.user.get_threads(user_id)
    except Exception as e:
        if "404" in str(e) or type(e).__name__ == "NotFoundError":
            return []
        raise
    own = [
        thread
        for thread in threads or []
        if thread_belongs_to(thread_id_of(thread), getattr(thread, "user_id", None), user_id)
    ]
    own.sort(key=lambda thread: _when(thread) or "", reverse=True)
    return own


def owns_thread(response: Any, thread_id: str, user_id: str) -> bool:
    """Whether a `thread.get` answer is for a thread of `user_id`.

    Zep names the owner on the answer itself, so checking costs no extra call;
    a legacy thread without one falls back to the anchored id match.
    """
    return thread_belongs_to(thread_id, getattr(response, "user_id", None), user_id)


def _preview(messages: list[Any]) -> str | None:
    """The user's first words in a session, else whatever was said first."""
    spoken = [m for m in messages if (getattr(m, "content", None) or "").strip()]
    first = next((m for m in spoken if role_of(m) == "user"), spoken[0] if spoken else None)
    if first is None:
        return None
    text = " ".join(first.content.split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1].rstrip() + "…"


async def describe_thread(client: AsyncZep, thread: Any) -> dict:
    """One session as the panel lists it: when, how long, how it opened.

    Two calls: the first few messages (which carry the total) and the last one.
    """
    thread_id = thread_id_of(thread) or ""
    head, tail = await asyncio.gather(
        client.thread.get(thread_id=thread_id, limit=PREVIEW_MESSAGES),
        client.thread.get(thread_id=thread_id, lastn=1),
    )
    first = messages_of(head)
    last = messages_of(tail)
    count = getattr(head, "total_count", None)
    if not isinstance(count, int) or count < len(first):
        count = len(first)
    return {
        "thread_id": thread_id,
        "created_at": _when(thread),
        "started_at": _when(first[0]) if first else None,
        "ended_at": _when(last[-1]) if last else (_when(first[-1]) if first else None),
        "message_count": count,
        "preview": _preview(first),
        "empty": not first,
    }


async def describe_threads(client: AsyncZep, threads: list[Any]) -> list[dict]:
    """`describe_thread` for each, a few at a time. A session Zep fails to
    describe is still listed, marked unavailable, rather than silently missing."""
    gate = asyncio.Semaphore(SESSION_CONCURRENCY)

    async def one(thread: Any) -> dict:
        async with gate:
            try:
                return await describe_thread(client, thread)
            except Exception as e:
                logger.warning("💬 Could not describe thread %s: %s", thread_id_of(thread), e)
                return {
                    "thread_id": thread_id_of(thread),
                    "created_at": _when(thread),
                    "started_at": None,
                    "ended_at": None,
                    "message_count": None,
                    "preview": None,
                    "empty": False,
                    "unavailable": True,
                }

    return list(await asyncio.gather(*(one(thread) for thread in threads)))
