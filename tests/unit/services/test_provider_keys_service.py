"""`src.services.provider_keys_service`: keys checked, encrypted, and kept to itself."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from src.services import provider_keys_service as p

pytestmark = pytest.mark.anyio

SECRET = Fernet.generate_key().decode()
USER = "user-1"
PASTED = "sk-test-1234567890"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(p.settings, "provider_keys_secret", SECRET, raising=False)
    monkeypatch.setattr(p.settings, "app_public_url", "https://api.test/", raising=False)


def _rows(fake_supabase):
    return fake_supabase.db.rows(p.TABLE)


class TestConnectKey:
    @respx.mock
    async def test_it_is_checked_then_stored_encrypted(self, fake_supabase):
        route = respx.get(p.CHECKS["anthropic"].url).mock(return_value=httpx.Response(200))
        assert await p.connect_key(USER, "anthropic", f"  {PASTED}\n") == {"hint": "7890"}

        sent = route.calls[0].request.headers
        assert sent["x-api-key"] == PASTED and sent["anthropic-version"]
        row = _rows(fake_supabase)[0]
        assert row["encrypted_key"] != PASTED and PASTED not in json.dumps(row)
        assert Fernet(SECRET.encode()).decrypt(row["encrypted_key"].encode()).decode() == PASTED

    @respx.mock
    async def test_a_bearer_provider_gets_a_bearer_header(self, fake_supabase):
        route = respx.get(p.CHECKS["groq"].url).mock(return_value=httpx.Response(200))
        await p.connect_key(USER, "groq", PASTED)
        assert route.calls[0].request.headers["authorization"] == f"Bearer {PASTED}"

    @respx.mock
    async def test_a_second_key_replaces_the_first(self, fake_supabase):
        respx.get(p.CHECKS["xai"].url).mock(return_value=httpx.Response(200))
        await p.connect_key(USER, "xai", PASTED)
        await p.connect_key(USER, "xai", "sk-another-key-9999")
        assert [r["key_hint"] for r in _rows(fake_supabase)] == ["9999"]

    @pytest.mark.parametrize("bad", ["short", "two words key here"])
    async def test_what_is_not_a_key_never_goes_out(self, bad):
        with respx.mock(assert_all_called=False) as router:
            route = router.get(p.CHECKS["xai"].url)
            with pytest.raises(p.InvalidKeyError):
                await p.connect_key(USER, "xai", bad)
            assert not route.called

    async def test_unknown_provider(self):
        with pytest.raises(p.UnknownProviderError):
            await p.connect_key(USER, "skynet", PASTED)

    async def test_without_the_secret_nothing_goes_out(self, monkeypatch):
        monkeypatch.setattr(p.settings, "provider_keys_secret", None, raising=False)
        with pytest.raises(p.ProviderKeysNotConfiguredError):
            await p.connect_key(USER, "xai", PASTED)

    @pytest.mark.parametrize("code", [400, 401, 403])
    @respx.mock
    async def test_a_refused_key_is_not_stored(self, fake_supabase, code):
        respx.get(p.CHECKS["xai"].url).mock(return_value=httpx.Response(code))
        with pytest.raises(p.InvalidKeyError):
            await p.connect_key(USER, "xai", PASTED)
        assert _rows(fake_supabase) == []

    @respx.mock
    async def test_a_provider_in_trouble_is_not_a_bad_key(self, fake_supabase):
        respx.get(p.CHECKS["xai"].url).mock(return_value=httpx.Response(503))
        with pytest.raises(p.ProviderUnavailableError):
            await p.connect_key(USER, "xai", PASTED)

    @respx.mock
    async def test_a_silent_provider_is_not_a_bad_key(self, fake_supabase):
        respx.get(p.CHECKS["xai"].url).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(p.ProviderUnavailableError):
            await p.connect_key(USER, "xai", PASTED)


class TestOwnership:
    async def test_disconnect_touches_only_your_own(self, fake_supabase):
        fake_supabase.db.seed(p.TABLE, {"user_id": "other", "provider": "xai"})
        assert await p.disconnect(USER, "xai") is False
        assert len(_rows(fake_supabase)) == 1

    async def test_the_list_shows_only_your_own(self, fake_supabase):
        fake_supabase.db.seed(
            p.TABLE,
            {
                "user_id": "other",
                "provider": "xai",
                "key_hint": "0000",
                "updated_at": "2026-11-02",
            },
        )
        assert (await p.list_connections(USER))["connected"] == {}


class TestOpenRouter:
    def _state(self, url: str) -> tuple[str, httpx.URL]:
        parsed = httpx.URL(url)
        return httpx.URL(parsed.params["callback_url"]).params["state"], parsed

    def test_the_url_carries_a_real_pkce_challenge(self):
        state, url = self._state(p.openrouter_authorization_url(USER))
        assert url.params["code_challenge_method"] == "S256"
        assert httpx.URL(url.params["callback_url"]).host == "api.test"
        user, verifier = p._read_state(state)
        assert user == USER
        digest = hashlib.sha256(verifier.encode()).digest()
        assert (
            base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == url.params["code_challenge"]
        )

    def test_the_verifier_is_not_readable_in_the_url(self):
        state, _ = self._state(p.openrouter_authorization_url(USER))
        assert USER not in state

    def test_no_public_url_is_not_configured(self, monkeypatch):
        monkeypatch.setattr(p.settings, "app_public_url", None, raising=False)
        with pytest.raises(p.ProviderKeysNotConfiguredError):
            p.openrouter_authorization_url(USER)

    @respx.mock
    async def test_a_code_becomes_a_stored_key(self, fake_supabase):
        route = respx.post(p.OPENROUTER_KEYS_URL).mock(
            return_value=httpx.Response(200, json={"key": "sk-or-abcd"})
        )
        state, _ = self._state(p.openrouter_authorization_url(USER))
        await p.complete_openrouter(state, "the-code")
        body = json.loads(route.calls[0].request.content)
        assert body["code"] == "the-code" and body["code_verifier"] == p._read_state(state)[1]
        row = _rows(fake_supabase)[0]
        assert (row["user_id"], row["provider"], row["key_hint"]) == (USER, "openrouter", "abcd")

    @pytest.mark.parametrize(
        "response",
        [httpx.Response(403), httpx.Response(200, json={}), httpx.Response(200, json={"key": ""})],
    )
    @respx.mock
    async def test_no_key_back_stores_nothing(self, fake_supabase, response):
        respx.post(p.OPENROUTER_KEYS_URL).mock(return_value=response)
        state, _ = self._state(p.openrouter_authorization_url(USER))
        with pytest.raises(p.InvalidKeyError):
            await p.complete_openrouter(state, "c")
        assert _rows(fake_supabase) == []

    @respx.mock
    async def test_openrouter_down(self, fake_supabase):
        respx.post(p.OPENROUTER_KEYS_URL).mock(side_effect=httpx.ConnectError("down"))
        state, _ = self._state(p.openrouter_authorization_url(USER))
        with pytest.raises(p.ProviderUnavailableError):
            await p.complete_openrouter(state, "c")

    async def test_a_forged_state_is_refused(self):
        with pytest.raises(p.InvalidKeyError):
            await p.complete_openrouter("forged", "c")
