from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

# Import the dependency to override
from src.api.routes.memory import get_zep_client
from src.main import app


@pytest.fixture
def mock_zep_client():
    client = AsyncMock()

    # Mock graph.search response
    mock_edge = MagicMock()
    mock_edge.fact = "User likes pizza"
    mock_edge.uuid = "edge-123"

    search_response = MagicMock()
    search_response.edges = [mock_edge]
    client.graph.search.return_value = search_response

    # Mock graph.node.get_by_user_id
    mock_node = MagicMock()
    mock_node.name = "User"
    mock_node.uuid = "node-123"
    mock_node.labels = ["Person"]
    mock_node.summary = "A test user"

    client.graph.node.get_by_user_id.return_value = [mock_node]

    return client


@pytest.fixture
def auth_client_with_zep(auth_client, mock_zep_client):
    """Authenticated client with mocked Zep service."""
    app.dependency_overrides[get_zep_client] = lambda: mock_zep_client
    yield auth_client
    # Clean up specific override (auth_client fixture cleans up its own)
    if get_zep_client in app.dependency_overrides:
        del app.dependency_overrides[get_zep_client]


@pytest.mark.anyio
async def test_get_user_facts(auth_client_with_zep: AsyncClient, mock_zep_client):
    """Test getting user facts."""
    response = await auth_client_with_zep.get("/memory/test-user-id/facts")
    assert response.status_code == 200
    data = response.json()
    assert data["facts"] == ["User likes pizza"]
    assert data["count"] == 1
    assert data["total"] == 1
    assert data["has_more"] is False

    # Verify Zep client was called correctly
    mock_zep_client.graph.search.assert_called_once()
    call_kwargs = mock_zep_client.graph.search.call_args.kwargs
    assert call_kwargs["user_id"] == "test-user-id"


@pytest.mark.anyio
async def test_get_user_nodes(auth_client_with_zep: AsyncClient, mock_zep_client):
    """Test getting user nodes."""
    response = await auth_client_with_zep.get("/memory/test-user-id/nodes")
    assert response.status_code == 200
    data = response.json()
    assert "nodes" in data
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["name"] == "User"


@pytest.mark.anyio
async def test_access_denied_other_user(auth_client_with_zep: AsyncClient):
    """Test user cannot access another user's memory."""
    response = await auth_client_with_zep.get("/memory/other-user-id/facts")
    assert response.status_code == 403
