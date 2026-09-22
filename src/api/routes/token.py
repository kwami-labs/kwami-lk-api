"""Token generation endpoints.

Note: Agent dispatching is handled by LiveKit Cloud's auto-dispatch feature.
Do NOT manually dispatch agents here to avoid duplicate agents in rooms.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.authz import require_kwami_owned
from src.api.deps import require_auth
from src.api.ratelimit import TOKEN_LIMIT, limiter
from src.core.config import settings
from src.core.security import AuthUser
from src.services.credits import get_balance
from src.services.livekit import create_token
from src.services.sessions import build_room_name, claim_room

logger = logging.getLogger("kwami-api.token")
router = APIRouter()


class TokenRequest(BaseModel):
    """Request body for token generation."""

    model_config = ConfigDict(populate_by_name=True)

    room_name: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        alias="roomName",
        description=(
            "Room to join. Optional: when omitted the server derives an unguessable "
            "name. A room name already issued to a different user is rejected."
        ),
    )
    participant_name: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        alias="participantName",
        description="Display name for participant",
    )
    participant_identity: str | None = Field(
        None,
        max_length=128,
        alias="participantIdentity",
        description="Unique identity (defaults to participant_name)",
    )

    # Permissions
    can_publish: bool = Field(
        True, alias="canPublish", description="Allow publishing audio/video tracks"
    )
    can_subscribe: bool = Field(
        True, alias="canSubscribe", description="Allow subscribing to tracks"
    )
    can_publish_data: bool = Field(
        True, alias="canPublishData", description="Allow publishing data messages"
    )

    # Kwami-specific metadata
    kwami_id: str | None = Field(
        None, alias="kwamiId", description="Kwami instance ID for agent matching"
    )


class TokenResponse(BaseModel):
    """Response containing the generated token."""

    token: str = Field(..., description="JWT access token")
    room_name: str = Field(..., description="Room name")
    participant_identity: str = Field(..., description="Participant identity")
    livekit_url: str = Field(..., description="LiveKit server URL to connect to")


@router.post("", response_model=TokenResponse)
@limiter.limit(TOKEN_LIMIT)
async def generate_token(
    request: Request,
    body: TokenRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Generate a LiveKit access token for a participant.

    This endpoint creates a JWT token that allows a client to connect
    to a LiveKit room with the specified permissions.

    Note: Agent dispatching is handled automatically by LiveKit Cloud's
    auto-dispatch feature when a user joins the room.

    Requires authentication - user's Supabase ID is used as participant identity.
    """
    # Use Supabase user ID as the identity (for memory persistence)
    identity = user.id
    # The participant name is embedded in the LiveKit token and is visible to
    # every other participant in the room. It used to default to the caller's
    # email address, which published a real identifier to anyone sharing a room
    # -- including the agent's other callers on a shared room. A caller may still
    # choose a display name; otherwise it is the opaque user id.
    participant_name = body.participant_name or identity

    # The kwami is dispatched to the agent as metadata and selects that kwami's
    # configuration, memory namespace, channels and wallet. It arrives from the
    # request body, so it must be proven to belong to the caller -- previously it
    # was passed straight through, letting any user run another user's kwami.
    if body.kwami_id:
        await require_kwami_owned(user.id, body.kwami_id)

    # Derive the room when the client does not name one; otherwise bind the
    # requested name to this user. `claim_room` raises 403 if the room was already
    # issued to somebody else, which is what stops a caller joining another
    # tenant's live session by naming their room.
    room_name = body.room_name or build_room_name(body.kwami_id)
    await claim_room(room_name, user_id=user.id, kwami_id=body.kwami_id, source="web")

    logger.info("📥 Token request: room=%s, user=%s", room_name, user.id)
    # Check credit balance before allowing connection
    try:
        credit_data = await get_balance(identity)
        if credit_data["balance"] <= 0:
            logger.warning(
                "🚫 User %s has insufficient credits (%s)", identity, credit_data["balance"]
            )
            raise HTTPException(
                status_code=402,
                detail="Insufficient credits. Please purchase credits to continue.",
            )
    except HTTPException:
        raise
    except Exception as e:
        if settings.credits_fail_open_on_check_error:
            logger.exception("Credit check failed, allowing connection")
        else:
            logger.exception("Credit check failed, blocking connection")
            raise HTTPException(
                status_code=503,
                detail="Credit verification is temporarily unavailable. Please try again shortly.",
            ) from e

    try:
        token = create_token(
            room_name=room_name,
            participant_name=participant_name,
            participant_identity=identity,
            can_publish=body.can_publish,
            can_subscribe=body.can_subscribe,
            can_publish_data=body.can_publish_data,
            kwami_id=body.kwami_id,
        )

        logger.info("🎫 Token generated for '%s' in room '%s'", identity, room_name)

        return TokenResponse(
            token=token,
            room_name=room_name,
            participant_identity=identity,
            livekit_url=settings.livekit_url,
        )

    except Exception as e:
        logger.exception("Failed to generate token")
        raise HTTPException(status_code=500, detail="Failed to generate token") from e


@router.get("", response_model=TokenResponse, deprecated=True)
@limiter.limit(TOKEN_LIMIT)
async def generate_token_get(
    request: Request,
    user: Annotated[AuthUser, Depends(require_auth)],
    room_name: Annotated[
        str | None, Query(alias="roomName", min_length=1, max_length=128, description="Room name")
    ] = None,
    participant_name: Annotated[
        str | None,
        Query(
            alias="participantName", min_length=1, max_length=128, description="Participant name"
        ),
    ] = None,
    kwami_id: Annotated[str | None, Query(alias="kwamiId", max_length=128)] = None,
):
    """
    Generate a LiveKit access token (GET method for simple integrations).

    **Deprecated.** A GET is expected to be safe and idempotent; this one claims a
    room, writes a `livekit_sessions` row and checks a credit balance, so a
    prefetch, a retry or a crawler is a state change. Everything here is
    available on the POST endpoint. Kept for existing integrations and marked
    deprecated in the schema so clients can see it going.

    Requires authentication.
    """
    body = TokenRequest(
        room_name=room_name,
        participant_name=participant_name,
        kwami_id=kwami_id,
    )
    # `generate_token` is wrapped by the limiter, so calling it here would bill
    # the caller twice for one request. `__wrapped__` is the undecorated handler.
    inner = getattr(generate_token, "__wrapped__", generate_token)
    return await inner(request, body, user)
