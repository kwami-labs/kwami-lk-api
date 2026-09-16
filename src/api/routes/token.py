"""Token generation endpoints.

Note: Agent dispatching is handled by LiveKit Cloud's auto-dispatch feature.
Do NOT manually dispatch agents here to avoid duplicate agents in rooms.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.api.authz import require_kwami_owned
from src.api.deps import require_auth
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
async def generate_token(
    request: TokenRequest,
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
    participant_name = user.email or request.participant_name or identity

    # The kwami is dispatched to the agent as metadata and selects that kwami's
    # configuration, memory namespace, channels and wallet. It arrives from the
    # request body, so it must be proven to belong to the caller -- previously it
    # was passed straight through, letting any user run another user's kwami.
    if request.kwami_id:
        require_kwami_owned(user.id, request.kwami_id)

    # Derive the room when the client does not name one; otherwise bind the
    # requested name to this user. `claim_room` raises 403 if the room was already
    # issued to somebody else, which is what stops a caller joining another
    # tenant's live session by naming their room.
    room_name = request.room_name or build_room_name(request.kwami_id)
    claim_room(room_name, user_id=user.id, kwami_id=request.kwami_id, source="web")

    logger.info(f"📥 Token request: room={room_name}, user={user.id}")

    # Check credit balance before allowing connection
    try:
        credit_data = await get_balance(identity)
        if credit_data["balance"] <= 0:
            logger.warning(
                f"🚫 User {identity} has insufficient credits ({credit_data['balance']})"
            )
            raise HTTPException(
                status_code=402,
                detail="Insufficient credits. Please purchase credits to continue.",
            )
    except HTTPException:
        raise
    except Exception as e:
        if settings.credits_fail_open_on_check_error:
            logger.error(f"Credit check failed, allowing connection: {e}")
        else:
            logger.error(f"Credit check failed, blocking connection: {e}")
            raise HTTPException(
                status_code=503,
                detail="Credit verification is temporarily unavailable. Please try again shortly.",
            ) from e

    try:
        token = create_token(
            room_name=room_name,
            participant_name=participant_name,
            participant_identity=identity,
            can_publish=request.can_publish,
            can_subscribe=request.can_subscribe,
            can_publish_data=request.can_publish_data,
            kwami_id=request.kwami_id,
        )

        logger.info(f"🎫 Token generated for '{identity}' in room '{room_name}'")

        return TokenResponse(
            token=token,
            room_name=room_name,
            participant_identity=identity,
            livekit_url=settings.livekit_url,
        )

    except Exception as e:
        logger.error(f"Failed to generate token: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate token")


@router.get("", response_model=TokenResponse)
async def generate_token_get(
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

    For production use, prefer the POST endpoint with full options.
    Requires authentication.
    """
    request = TokenRequest(
        room_name=room_name,
        participant_name=participant_name,
        kwami_id=kwami_id,
    )
    return await generate_token(request, user)
