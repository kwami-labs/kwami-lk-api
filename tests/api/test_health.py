import pytest
from httpx import AsyncClient

from src import __version__
from src.core.config import settings


@pytest.mark.anyio
async def test_health_check(client: AsyncClient):
    """Test health endpoint returns healthy status."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "kwami-lk-api"


@pytest.mark.anyio
async def test_root(client: AsyncClient):
    """Test root endpoint returns API info."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Kwami AI LiveKit API"
    assert data["version"] == __version__
    assert "docs" in data


@pytest.mark.anyio
async def test_the_reported_version_is_the_packages_own(client: AsyncClient):
    """This asserted only `"version" in data`, which is why a hardcoded "0.1.0"
    survived a 100%-covered suite while the package said "0.1.1"."""
    data = (await client.get("/")).json()
    assert data["version"] == __version__
    assert data["version"] != "0.1.0" or __version__ == "0.1.0"


@pytest.mark.anyio
async def test_docs_is_null_when_the_uis_are_closed(client: AsyncClient, monkeypatch):
    """Advertising /docs while it 404s sends every reader to a dead link."""
    monkeypatch.setattr(settings, "app_env", "production", raising=False)
    monkeypatch.setattr(settings, "enable_docs", False, raising=False)
    assert (await client.get("/")).json()["docs"] is None


@pytest.mark.anyio
async def test_docs_is_advertised_when_open(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development", raising=False)
    assert (await client.get("/")).json()["docs"] == "/docs"
