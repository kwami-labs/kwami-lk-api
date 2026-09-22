"""Ownership of LiveKit rooms.

``POST /token`` took the room name from the request body and minted a token for
it unconditionally, so any authenticated user could join any room by guessing or
observing its name. Rooms are now recorded when a token is issued, and a room
belongs to the first user it was issued to -- enforced by the UNIQUE constraint on
``livekit_sessions.room_name``, not by a read-then-write in Python.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from src.core.errors import ForbiddenError
from src.services.credits import get_supabase_admin

logger = logging.getLogger("kwami-api.sessions")

# Matches the shape telephony.build_call_room_name already produces for SIP calls.
ROOM_NAME_PREFIX = "kwami-web"


def build_room_name(kwami_id: str | None) -> str:
    """Server-side room name. Unguessable, and namespaced by kwami."""
    prefix = f"{ROOM_NAME_PREFIX}-{kwami_id[:8]}" if kwami_id else ROOM_NAME_PREFIX
    return f"{prefix}-{uuid4().hex[:12]}"


async def get_session(room_name: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    result = (
        await sb.table("livekit_sessions")
        .select("id, room_name, user_id, kwami_id, source, status")
        .eq("room_name", room_name)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


async def claim_room(
    room_name: str,
    *,
    user_id: str,
    kwami_id: str | None,
    source: str = "web",
) -> dict[str, Any]:
    """Record the room for this user, or confirm they already own it.

    Raises ``ForbiddenError`` when the room belongs to someone else. That is the check
    that closes the cross-tenant join: an attacker who learns a room name cannot
    obtain a token for it, because the row already names a different owner.
    """
    existing = await get_session(room_name)
    if existing is not None:
        if str(existing.get("user_id")) != str(user_id):
            logger.warning(
                "Rejected token request for room owned by another user (room=%s requester=%s)",
                room_name,
                user_id,
            )
            raise ForbiddenError("This room belongs to another user")
        return existing

    sb = get_supabase_admin()
    payload = {
        "room_name": room_name,
        "user_id": user_id,
        "kwami_id": kwami_id,
        "source": source,
        "status": "issued",
    }
    try:
        result = await sb.table("livekit_sessions").insert(payload).execute()
    except Exception as exc:
        # UNIQUE(room_name) is the real arbiter: on a race, whoever lost re-reads
        # and is accepted only if they are the owner.
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            existing = await get_session(room_name)
            if existing and str(existing.get("user_id")) == str(user_id):
                return existing
            raise ForbiddenError("This room belongs to another user") from exc
        raise
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else payload
