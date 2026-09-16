"""LiveKit utilities for token generation."""

from datetime import timedelta

from livekit import api
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.room import RoomConfiguration

from src.core.config import settings


def _default_agent_name() -> str:
    """The agent to dispatch.

    This was a module constant duplicating `settings.livekit_agent_name`, which
    telephony.py already used -- two sources of truth for the same value, so a web
    session and a phone call could dispatch different agents.
    """
    return settings.livekit_agent_name


def create_token(
    room_name: str,
    participant_name: str,
    *,
    participant_identity: str | None = None,
    ttl: timedelta | None = None,
    can_publish: bool = True,
    can_subscribe: bool = True,
    can_publish_data: bool = True,
    can_update_own_metadata: bool = True,
    room_join: bool = True,
    room_create: bool = False,
    room_admin: bool = False,
    agent: bool = False,
    dispatch_agent: bool = True,
    agent_name: str | None = None,
    kwami_id: str | None = None,
) -> str:
    """
    Create a LiveKit access token.

    Args:
        room_name: Name of the room to join
        participant_name: Display name for the participant
        participant_identity: Unique identity (defaults to participant_name)
        ttl: Token time-to-live (defaults to LIVEKIT_TOKEN_TTL_MINUTES)
        can_publish: Allow publishing tracks
        can_subscribe: Allow subscribing to tracks
        can_publish_data: Allow publishing data messages
        can_update_own_metadata: Allow updating own metadata
        room_join: Allow joining rooms
        room_create: Allow creating rooms
        room_admin: Admin privileges
        agent: Whether this token is for an agent
        dispatch_agent: Whether to dispatch the Kwami agent to the room
        agent_name: Name of the agent to dispatch (defaults to kwami-agent)
        kwami_id: Kwami instance ID to pass to the agent

    Returns:
        JWT token string
    """
    identity = participant_identity or participant_name
    token_ttl = ttl or timedelta(minutes=settings.livekit_token_ttl_minutes)

    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(participant_name)
        .with_ttl(token_ttl)
        .with_grants(
            api.VideoGrants(
                room=room_name,
                room_join=room_join,
                room_create=room_create,
                room_admin=room_admin,
                can_publish=can_publish,
                can_subscribe=can_subscribe,
                can_publish_data=can_publish_data,
                can_update_own_metadata=can_update_own_metadata,
                agent=agent,
            )
        )
    )

    # Dispatch the Kwami agent to the room when participant joins
    if dispatch_agent:
        target_agent = agent_name or _default_agent_name()
        metadata = kwami_id or ""
        token = token.with_room_config(
            RoomConfiguration(
                agents=[RoomAgentDispatch(agent_name=target_agent, metadata=metadata)],
            ),
        )

    return token.to_jwt()
