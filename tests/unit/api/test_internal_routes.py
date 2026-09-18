"""`/internal` — the agent's key-authenticated bootstrap surface.

Every route here is reachable only with the shared `X-Kwami-API-Key`, and none
of them are scoped by an end user, so the guard is the whole access-control
story. The browser-context routes deliberately expose no way to enumerate
owners: a caller must already know the owner key.

The runtime-config route wraps everything in a catch-all that turns any failure
into a 500 carrying the exception text, which is covered here because it is the
one place in the file where upstream text reaches the client.
"""

from __future__ import annotations

import pytest

from src.api.routes import internal as internal_route
from src.services.browser_sessions import UnsupportedVendorError
from src.services.channels import build_agent_bootstrap_payload

pytestmark = pytest.mark.anyio

KEY = "internal-secret"


@pytest.fixture(autouse=True)
def _keyed(monkeypatch):
    from src.api import deps

    monkeypatch.setattr(deps.settings, "kwami_api_key", KEY, raising=False)


@pytest.fixture
def keyed_client(client):
    client.headers["X-Kwami-API-Key"] = KEY
    return client


class TestAuthGuard:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/internal/kwamis/k1/runtime"),
            ("get", "/internal/channels/by-address?address=%2B14155552671"),
            ("get", "/internal/browser-contexts/owner-1?vendor=browserbase"),
            ("post", "/internal/browser-contexts/owner-1"),
            ("delete", "/internal/browser-contexts/owner-1"),
        ],
    )
    async def test_every_route_needs_the_key(self, client, method, path):
        # httpx's get/delete take no `json=`, so only POST carries a body.
        kwargs = {"json": {}} if method == "post" else {}
        r = await getattr(client, method)(path, **kwargs)
        assert r.status_code == 401

    async def test_a_wrong_key_is_refused(self, client):
        client.headers["X-Kwami-API-Key"] = "not-the-key"
        assert (await client.get("/internal/kwamis/k1/runtime")).status_code == 401


class TestRuntimeConfig:
    async def test_it_returns_the_bootstrap_payload(self, keyed_client, tenant, fake_supabase):
        r = await keyed_client.get(f"/internal/kwamis/{tenant.kwami_id}/runtime")
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "config"
        assert body["kwamiId"] == tenant.kwami_id
        assert "voice" in body and "soul" in body and "memory" in body

    async def test_an_unknown_kwami_is_a_404(self, keyed_client, fake_supabase):
        r = await keyed_client.get("/internal/kwamis/00000000-0000-0000-0000-000000000000/runtime")
        assert r.status_code == 404
        assert r.json()["detail"] == "Kwami not found"

    async def test_it_is_not_scoped_by_user(self, keyed_client, tenant, other_tenant):
        """No end user to scope by -- the key is the whole guard."""
        for kwami_id in (tenant.kwami_id, other_tenant.kwami_id):
            assert (
                await keyed_client.get(f"/internal/kwamis/{kwami_id}/runtime")
            ).status_code == 200

    async def test_a_database_failure_is_a_500(self, monkeypatch, keyed_client):
        from src.services import credits as credits_mod

        def boom():
            raise RuntimeError("supabase down")

        monkeypatch.setattr(credits_mod, "get_supabase_admin", boom)
        r = await keyed_client.get("/internal/kwamis/k1/runtime")
        assert r.status_code == 500
        assert "Failed to load runtime config" in r.json()["detail"]


class TestBuildAgentBootstrapPayload:
    def test_an_empty_config_yields_every_default(self):
        payload = build_agent_bootstrap_payload({"id": "k1", "name": None, "config": None})
        assert payload["kwamiName"] == "Kwami"
        assert payload["voice"]["stt"] == {
            "provider": "deepgram",
            "model": "nova-2-phonecall",
            "language": "en",
        }
        assert payload["voice"]["llm"]["provider"] == "openai"
        assert payload["voice"]["llm"]["model"] == "gpt-4o-mini"
        assert payload["voice"]["llm"]["temperature"] == 0.7
        assert payload["voice"]["llm"]["maxTokens"] == 1024
        assert payload["voice"]["tts"] == {
            "provider": "openai",
            "model": "tts-1",
            "voice": "nova",
            "speed": 1.0,
        }
        assert payload["soul"]["conversationStyle"] == "friendly"
        assert payload["soul"]["responseLength"] == "medium"
        assert payload["soul"]["emotionalTone"] == "warm"
        assert payload["soul"]["traits"] == []
        assert payload["memory"] == {"enabled": True}

    def test_a_saved_config_overrides_the_defaults(self):
        payload = build_agent_bootstrap_payload(
            {
                "id": "k1",
                "name": "Nova",
                "config": {
                    "voice": {
                        "stt": {"provider": "cartesia", "model": "ink", "language": "fr"},
                        "llm": {
                            "provider": "anthropic",
                            "model": "claude",
                            "temperature": 0.1,
                            "maxTokens": 4096,
                        },
                        "tts": {
                            "provider": "elevenlabs",
                            "model": "v2",
                            "voice": "rachel",
                            "speed": 1.2,
                        },
                        "soulConfig": {
                            "name": "Soul",
                            "personality": "curious",
                            "systemPrompt": "Be brief.",
                            "traits": ["kind"],
                            "conversationStyle": "terse",
                            "responseLength": "short",
                            "emotionalTone": "cool",
                        },
                    }
                },
            }
        )
        assert payload["kwamiName"] == "Nova"
        assert payload["voice"]["stt"]["language"] == "fr"
        assert payload["voice"]["llm"]["maxTokens"] == 4096
        assert payload["voice"]["tts"]["voice"] == "rachel"
        assert payload["soul"]["name"] == "Soul"
        assert payload["soul"]["traits"] == ["kind"]

    def test_the_soul_name_falls_back_to_the_kwami_name(self):
        payload = build_agent_bootstrap_payload(
            {"id": "k1", "name": "Nova", "config": {"voice": {"soulConfig": {}}}}
        )
        assert payload["soul"]["name"] == "Nova"

    def test_a_zero_temperature_falls_back_to_the_default(self):
        """`or` on a numeric setting: 0.0 is falsey, so it cannot be selected."""
        payload = build_agent_bootstrap_payload(
            {"id": "k1", "name": "N", "config": {"voice": {"llm": {"temperature": 0}}}}
        )
        assert payload["voice"]["llm"]["temperature"] == 0.7, "a real 0 is unreachable"


class TestChannelByAddress:
    async def test_it_finds_a_channel_by_phone_number(
        self, keyed_client, fake_supabase, tenant_factory
    ):
        tenant_factory(email="c@example.com", with_channel=True, phone_number="+14155552671")
        r = await keyed_client.get(
            "/internal/channels/by-address", params={"address": "+14155552671"}
        )
        assert r.status_code == 200
        assert r.json()["channel"]["phone_number"] == "+14155552671"

    async def test_an_unknown_address_is_a_404(self, keyed_client, fake_supabase):
        r = await keyed_client.get(
            "/internal/channels/by-address", params={"address": "+19999999999"}
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "Channel not found"

    async def test_the_address_is_required(self, keyed_client):
        assert (await keyed_client.get("/internal/channels/by-address")).status_code == 422


class TestBrowserContextRoutes:
    OWNER = "user-1"

    async def test_saving_then_reading_round_trips(self, keyed_client, fake_supabase):
        saved = await keyed_client.post(
            f"/internal/browser-contexts/{self.OWNER}",
            json={"vendor": "browserbase", "context_id": "ctx-1"},
        )
        assert saved.status_code == 200
        assert saved.json() == {
            "owner_key": self.OWNER,
            "vendor": "browserbase",
            "saved": True,
        }

        read = await keyed_client.get(
            f"/internal/browser-contexts/{self.OWNER}", params={"vendor": "browserbase"}
        )
        assert read.status_code == 200
        assert read.json()["context_id"] == "ctx-1"

    async def test_reading_an_unsaved_owner_is_a_404(self, keyed_client, fake_supabase):
        r = await keyed_client.get(
            "/internal/browser-contexts/nobody", params={"vendor": "browserbase"}
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "No saved browser context"

    async def test_the_vendor_is_required_on_read(self, keyed_client):
        assert (
            await keyed_client.get(f"/internal/browser-contexts/{self.OWNER}")
        ).status_code == 422

    @pytest.mark.parametrize(
        ("route_fn", "exc", "status"),
        [
            ("get_browser_context", UnsupportedVendorError("nope"), 400),
            ("get_browser_context", ValueError("owner_key is required"), 400),
            ("get_browser_context", RuntimeError("supabase down"), 500),
        ],
    )
    async def test_read_failures_map_to_status_codes(
        self, monkeypatch, keyed_client, route_fn, exc, status
    ):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(internal_route, route_fn, boom)
        r = await keyed_client.get(
            f"/internal/browser-contexts/{self.OWNER}", params={"vendor": "browserbase"}
        )
        assert r.status_code == status

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (UnsupportedVendorError("nope"), 400),
            (ValueError("context_id is required"), 400),
            (RuntimeError("supabase down"), 500),
        ],
    )
    async def test_save_failures_map_to_status_codes(self, monkeypatch, keyed_client, exc, status):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(internal_route, "save_browser_context", boom)
        r = await keyed_client.post(
            f"/internal/browser-contexts/{self.OWNER}",
            json={"vendor": "browserbase", "context_id": "ctx-1"},
        )
        assert r.status_code == status

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (UnsupportedVendorError("nope"), 400),
            (ValueError("owner_key is required"), 400),
            (RuntimeError("supabase down"), 500),
        ],
    )
    async def test_delete_failures_map_to_status_codes(
        self, monkeypatch, keyed_client, exc, status
    ):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(internal_route, "delete_browser_context", boom)
        r = await keyed_client.delete(f"/internal/browser-contexts/{self.OWNER}")
        assert r.status_code == status

    @pytest.mark.parametrize(
        "body",
        [
            {"vendor": "", "context_id": "c"},
            {"vendor": "b", "context_id": ""},
            {"vendor": "x" * 65, "context_id": "c"},
            {"vendor": "b", "context_id": "x" * 256},
            {"context_id": "c"},
            {"vendor": "b"},
        ],
    )
    async def test_a_malformed_save_body_is_a_422(self, keyed_client, body):
        r = await keyed_client.post(f"/internal/browser-contexts/{self.OWNER}", json=body)
        assert r.status_code == 422

    async def test_deleting_removes_the_pointer(self, keyed_client, fake_supabase):
        await keyed_client.post(
            f"/internal/browser-contexts/{self.OWNER}",
            json={"vendor": "browserbase", "context_id": "ctx-1"},
        )
        r = await keyed_client.delete(f"/internal/browser-contexts/{self.OWNER}")
        assert r.status_code == 200
        assert r.json()["removed"] >= 1
        assert (
            await keyed_client.get(
                f"/internal/browser-contexts/{self.OWNER}", params={"vendor": "browserbase"}
            )
        ).status_code == 404

    async def test_deleting_an_unsaved_owner_removes_nothing(self, keyed_client, fake_supabase):
        r = await keyed_client.delete("/internal/browser-contexts/nobody")
        assert r.status_code == 200
        assert r.json()["removed"] == 0
