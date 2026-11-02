"""`/providers`: status codes, and that a key never comes back out."""

from __future__ import annotations

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from src.services import provider_keys_service as p

pytestmark = pytest.mark.anyio

KEY = Fernet.generate_key().decode()
PASTED = "sk-test-1234567890"


@pytest.fixture
def keys_on(monkeypatch):
    monkeypatch.setattr(p.settings, "provider_keys_secret", KEY, raising=False)
    monkeypatch.setattr(p.settings, "app_public_url", "https://api.test/", raising=False)


@pytest.fixture
def keys_off(monkeypatch):
    monkeypatch.setattr(p.settings, "provider_keys_secret", None, raising=False)
    monkeypatch.setattr(p.settings, "app_public_url", "https://api.test/", raising=False)


class TestConnections:
    async def test_needs_a_login(self, client):
        assert (await client.get("/providers")).status_code == 401

    async def test_unconfigured_lists_nothing_and_says_so(self, tenant_client, keys_off):
        body = (await tenant_client.get("/providers")).json()
        assert body["enabled"] is False and body["connected"] == {}
        assert "xai" in body["providers"]

    @respx.mock
    async def test_a_connected_key_shows_only_its_end(self, tenant_client, keys_on):
        respx.get(p.CHECKS["xai"].url).mock(return_value=httpx.Response(200, json={}))
        r = await tenant_client.put("/providers/xai/key", json={"key": PASTED})
        assert r.status_code == 200 and r.json() == {"hint": "7890"}

        r = await tenant_client.get("/providers")
        assert r.json()["connected"]["xai"]["hint"] == "7890"
        assert PASTED not in r.text

    @respx.mock
    async def test_a_disconnected_key_is_gone(self, tenant_client, keys_on):
        respx.get(p.CHECKS["xai"].url).mock(return_value=httpx.Response(200, json={}))
        await tenant_client.put("/providers/xai/key", json={"key": PASTED})
        assert (await tenant_client.delete("/providers/xai")).status_code == 200
        assert (await tenant_client.get("/providers")).json()["connected"] == {}
        assert (await tenant_client.delete("/providers/xai")).status_code == 404


class TestConnectKey:
    async def test_cannot_store_is_a_503(self, tenant_client, keys_off):
        r = await tenant_client.put("/providers/xai/key", json={"key": PASTED})
        assert r.status_code == 503

    async def test_unknown_provider_is_a_404(self, tenant_client, keys_on):
        r = await tenant_client.put("/providers/skynet/key", json={"key": PASTED})
        assert r.status_code == 404

    @respx.mock
    async def test_a_refused_key_is_a_400_with_no_upstream_text(self, tenant_client, keys_on):
        respx.get(p.CHECKS["openai"].url).mock(return_value=httpx.Response(401, text="secret-x"))
        r = await tenant_client.put("/providers/openai/key", json={"key": PASTED})
        assert r.status_code == 400 and "secret-x" not in r.text and PASTED not in r.text

    @respx.mock
    async def test_a_silent_provider_is_a_502(self, tenant_client, keys_on):
        respx.get(p.CHECKS["openai"].url).mock(return_value=httpx.Response(500))
        r = await tenant_client.put("/providers/openai/key", json={"key": PASTED})
        assert r.status_code == 502


class TestOpenRouter:
    async def test_connect_needs_a_login(self, client, keys_on):
        assert (await client.post("/providers/openrouter/connect")).status_code == 401

    async def test_connect_gives_the_consent_url(self, tenant_client, keys_on):
        r = await tenant_client.post("/providers/openrouter/connect")
        assert r.status_code == 200 and r.json()["url"].startswith(p.OPENROUTER_AUTH_URL)

    async def test_connect_unconfigured_is_a_503(self, tenant_client, keys_off):
        assert (await tenant_client.post("/providers/openrouter/connect")).status_code == 503

    async def test_no_code_means_no_consent(self, client, keys_on):
        r = await client.get("/providers/openrouter/callback", params={"state": "x"})
        assert r.status_code == 400 and "No se ha conectado" in r.text

    async def test_a_forged_state_connects_nothing(self, client, keys_on):
        r = await client.get(
            "/providers/openrouter/callback", params={"state": "forged", "code": "c"}
        )
        assert r.status_code == 400

    @respx.mock
    async def test_the_callback_connects(self, client, keys_on, tenant):
        respx.post(p.OPENROUTER_KEYS_URL).mock(
            return_value=httpx.Response(200, json={"key": "sk-or-abcd"})
        )
        url = httpx.URL(p.openrouter_authorization_url(tenant.user_id))
        callback = httpx.URL(url.params["callback_url"])
        r = await client.get(
            "/providers/openrouter/callback",
            params={"state": callback.params["state"], "code": "c"},
        )
        assert r.status_code == 200 and "conectado" in r.text
