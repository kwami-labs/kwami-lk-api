import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_generate_token_post(auth_client: AsyncClient):
    """Test token generation via POST with authenticated user."""
    response = await auth_client.post(
        "/token",
        json={
            "roomName": "test-room",
            "participantName": "test-user",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["room_name"] == "test-room"
    # Identity should match the mocked auth user id
    assert data["participant_identity"] == "test-user-id"


@pytest.mark.anyio
async def test_generate_token_get(auth_client: AsyncClient):
    """Test token generation via GET with authenticated user."""
    response = await auth_client.get(
        "/token",
        params={
            "roomName": "test-room",
            "participantName": "test-user",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["participant_identity"] == "test-user-id"


@pytest.mark.anyio
async def test_generate_token_no_auth(client: AsyncClient):
    """Test token generation fails without authentication (if auth is enforced)."""
    # Note: If auth_enabled is False in config, get_current_user returns None.
    # But require_auth raises 401 if user is None.
    # So this should return 401.

    response = await client.post(
        "/token",
        json={
            "roomName": "test-room",
        },
    )
    assert response.status_code == 401
