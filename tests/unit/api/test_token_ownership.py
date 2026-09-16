"""`POST /token` must not mint a token for another tenant's kwami or room.

Before this, `roomName` and `kwamiId` were taken from the request body and used
unchecked: any authenticated user could join another user's live room, and could
have an agent dispatched running someone else's kwami configuration, memory
namespace, channels and wallet. Every other domain in the codebase validated
against `user_kwamis`; the flagship endpoint did not.
"""

import jwt
import pytest
from httpx import AsyncClient
from livekit.api import TokenVerifier

from src.core.config import settings


@pytest.mark.anyio
async def test_issues_a_token_for_a_kwami_the_caller_owns(tenant_client: AsyncClient, tenant):
    response = await tenant_client.post("/token", json={"kwamiId": tenant.kwami_id})
    assert response.status_code == 200
    body = response.json()

    # Verify the real grants rather than trusting the response body.
    claims = TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret).verify(
        body["token"]
    )
    assert claims.identity == tenant.user_id
    assert claims.video.room == body["room_name"]
    assert claims.video.room_join is True
    assert claims.video.room_admin is False, "a participant must not get admin on the room"
    assert claims.video.room_create is False


@pytest.mark.anyio
async def test_rejects_a_kwami_owned_by_someone_else(tenant_client: AsyncClient, other_tenant):
    """The core IDOR: another tenant's kwami_id must not be dispatchable."""
    response = await tenant_client.post("/token", json={"kwamiId": other_tenant.kwami_id})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "kwami_not_found"


@pytest.mark.anyio
async def test_rejects_a_kwami_that_does_not_exist(tenant_client: AsyncClient):
    response = await tenant_client.post(
        "/token", json={"kwamiId": "00000000-0000-0000-0000-000000000000"}
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_derives_an_unguessable_room_when_none_is_given(tenant_client: AsyncClient, tenant):
    first = await tenant_client.post("/token", json={"kwamiId": tenant.kwami_id})
    second = await tenant_client.post("/token", json={"kwamiId": tenant.kwami_id})

    assert first.status_code == second.status_code == 200
    room_a, room_b = first.json()["room_name"], second.json()["room_name"]
    assert room_a != room_b, "each session must get its own room"
    assert room_a.startswith("kwami-web-")
    assert len(room_a) > len("kwami-web-") + 12


@pytest.mark.anyio
async def test_cannot_join_a_room_claimed_by_another_user(
    tenant_client: AsyncClient,
    other_tenant_client: AsyncClient,
    tenant,
    other_tenant,
):
    """Knowing a room name must not be enough to get a token for it."""
    issued = await tenant_client.post(
        "/token", json={"roomName": "shared-room-name", "kwamiId": tenant.kwami_id}
    )
    assert issued.status_code == 200

    intruder = await other_tenant_client.post(
        "/token", json={"roomName": "shared-room-name", "kwamiId": other_tenant.kwami_id}
    )
    assert intruder.status_code == 403
    assert intruder.json()["error"]["code"] == "forbidden"


@pytest.mark.anyio
async def test_owner_can_rejoin_their_own_room(tenant_client: AsyncClient, tenant):
    """Reconnecting to a room you already own must keep working."""
    payload = {"roomName": "my-own-room", "kwamiId": tenant.kwami_id}
    assert (await tenant_client.post("/token", json=payload)).status_code == 200
    assert (await tenant_client.post("/token", json=payload)).status_code == 200


@pytest.mark.anyio
async def test_token_ttl_is_short(tenant_client: AsyncClient, tenant):
    """A leaked token should expire in minutes, not the previous six hours."""
    response = await tenant_client.post("/token", json={"kwamiId": tenant.kwami_id})
    claims = jwt.decode(
        response.json()["token"],
        settings.livekit_api_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    # LiveKit stamps `nbf`, not `iat`.
    lifetime = claims["exp"] - claims["nbf"]
    assert lifetime <= 60 * 60, "token lifetime should be at most an hour"
    assert lifetime == settings.livekit_token_ttl_minutes * 60


@pytest.mark.anyio
async def test_unauthenticated_requests_are_rejected(client: AsyncClient):
    response = await client.post("/token", json={"roomName": "r", "kwamiId": "k"})
    assert response.status_code == 401
