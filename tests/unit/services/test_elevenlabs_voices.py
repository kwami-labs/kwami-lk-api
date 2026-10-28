"""`src.services.elevenlabs_voices` — the account's own ElevenLabs voices.

What matters is what the picker may offer: a free plan answers 402 for library
voices, so those must never reach it, and any failure must fall back to the
YAML list instead of an error.
"""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from src.services import elevenlabs_voices as el
from src.services.voices import get_tts_voices

pytestmark = pytest.mark.anyio


def raw(voice_id: str, name: str, category: str, **labels) -> dict:
    return {
        "voice_id": voice_id,
        "name": name,
        "category": category,
        "labels": labels,
        "preview_url": f"https://cdn.test/{voice_id}.mp3",
    }


ACCOUNT = {
    "voices": [
        raw(
            "george",
            "George - Warm, Captivating Storyteller",
            "premade",
            gender="male",
            language="en",
            accent="british",
        ),
        raw(
            "carolina",
            "Carolina - Spanish woman",
            "professional",
            gender="female",
            language="es",
            accent="peninsular",
        ),
        raw("mine", "Mayordomo", "generated", gender="male", language="es"),
        {"name": "no id"},
    ]
}


def serve(monkeypatch, routes: dict) -> list[str]:
    """Answer `_get_json` from `routes` (path -> payload or exception); log the calls."""
    calls: list[str] = []

    async def fake(_session, path):
        calls.append(path)
        answer = routes[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(el, "_get_json", fake)
    return calls


def http_error(status: int) -> aiohttp.ClientResponseError:
    url = URL("https://api.elevenlabs.io/v1/voices")
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    return aiohttp.ClientResponseError(info, (), status=status)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    el.clear_cache()
    monkeypatch.setattr(el, "get_settings", lambda: SimpleNamespace(elevenlabs_api_key="k"))
    yield
    el.clear_cache()


class TestToVoice:
    def test_a_stock_voice_splits_its_tagline_off_the_name(self):
        v = el.to_voice(ACCOUNT["voices"][0])
        assert v is not None
        assert (v.id, v.name, v.category, v.gender, v.language) == (
            "george",
            "George",
            "Male",
            "male",
            "en",
        )
        assert v.description == "Warm, Captivating Storyteller (british)"
        assert v.preview_url == "https://cdn.test/george.mp3"

    def test_the_accounts_own_voices_are_grouped_as_custom(self):
        v = el.to_voice(ACCOUNT["voices"][2])
        assert v is not None and v.category == "Custom" and v.name == "Mayordomo"

    def test_an_entry_without_an_id_is_skipped(self):
        assert el.to_voice({"name": "x"}) is None

    def test_an_unknown_gender_is_dropped(self):
        v = el.to_voice(raw("x", "X", "premade", gender="other"))
        assert v is not None and v.gender is None and v.category == "Other"


class TestUsableOn:
    @pytest.mark.parametrize("tier", ["free", None])
    def test_free_or_unknown_plans_skip_library_voices(self, tier):
        assert el.usable_on(tier, {"category": "premade"})
        assert el.usable_on(tier, {"category": "generated"})
        assert not el.usable_on(tier, {"category": "professional"})

    def test_a_paid_plan_keeps_everything(self):
        assert el.usable_on("starter", {"category": "professional"})


class TestAccountVoices:
    async def test_a_free_plan_lists_stock_and_own_voices_own_first(self, monkeypatch):
        serve(monkeypatch, {"/voices": ACCOUNT, "/user/subscription": {"tier": "free"}})
        voices = await el.account_voices()
        assert [v.id for v in voices or []] == ["mine", "george"]

    async def test_a_paid_plan_also_lists_library_voices(self, monkeypatch):
        serve(monkeypatch, {"/voices": ACCOUNT, "/user/subscription": {"tier": "creator"}})
        voices = await el.account_voices()
        assert {v.id for v in voices or []} == {"mine", "george", "carolina"}

    async def test_an_unreadable_plan_counts_as_free(self, monkeypatch):
        serve(monkeypatch, {"/voices": ACCOUNT, "/user/subscription": http_error(401)})
        voices = await el.account_voices()
        assert "carolina" not in {v.id for v in voices or []}

    async def test_the_answer_is_cached(self, monkeypatch):
        calls = serve(monkeypatch, {"/voices": ACCOUNT, "/user/subscription": {"tier": "free"}})
        first = await el.account_voices()
        second = await el.account_voices()
        assert first == second
        assert calls == ["/voices", "/user/subscription"]

    async def test_a_failure_falls_back_and_is_not_retried_at_once(self, monkeypatch):
        calls = serve(monkeypatch, {"/voices": http_error(500)})
        assert await el.account_voices() is None
        assert await el.account_voices() is None
        assert calls == ["/voices"]

    async def test_a_timeout_falls_back(self, monkeypatch):
        serve(monkeypatch, {"/voices": TimeoutError()})
        assert await el.account_voices() is None

    async def test_without_a_key_nothing_is_requested(self, monkeypatch):
        monkeypatch.setattr(el, "get_settings", lambda: SimpleNamespace(elevenlabs_api_key=None))
        calls = serve(monkeypatch, {})
        assert await el.account_voices() is None
        assert calls == []

    async def test_an_empty_account_falls_back(self, monkeypatch):
        serve(monkeypatch, {"/voices": {"voices": []}, "/user/subscription": {"tier": "free"}})
        assert await el.account_voices() is None


class TestCatalogue:
    def test_the_live_list_replaces_the_yaml_one(self):
        voice = el.to_voice(ACCOUNT["voices"][2])
        assert voice is not None
        provider = get_tts_voices([voice])["elevenlabs"]
        assert provider.source == "live" and [v.id for v in provider.voices] == ["mine"]

    def test_without_it_the_yaml_list_stands_in(self):
        provider = get_tts_voices()["elevenlabs"]
        assert provider.source == "yaml"
        assert all(v.preview_url for v in provider.voices), "every stock voice has a sample"

    def test_gemini_tts_speaks_with_the_gemini_live_voices(self):
        providers = get_tts_voices()
        assert providers["gemini"].source == "sdk"
        assert "Charon" in {v.id for v in providers["gemini"].voices}
        assert "en-US-Studio-O" in {v.id for v in providers["google"].voices}, (
            "google stays Google Cloud TTS"
        )


class TestDescriptions:
    def test_an_accent_alone_is_the_description(self):
        v = el.to_voice(raw("x", "Plain", "premade", accent="british"))
        assert v is not None and v.description == "british"

    def test_the_descriptive_label_stands_in_for_a_missing_tagline(self):
        v = el.to_voice(raw("x", "Plain", "premade", descriptive="calm"))
        assert v is not None and v.description == "calm"

    def test_no_labels_means_no_description(self):
        v = el.to_voice(raw("x", "Plain", "premade"))
        assert v is not None and v.description is None and v.language is None

    def test_a_name_that_is_only_a_tagline_keeps_the_full_name(self):
        v = el.to_voice(raw("x", " - Tagline", "premade"))
        assert v is not None and v.name == " - Tagline"


class _Response:
    def __init__(self, payload, error=None):
        self._payload, self._error = payload, error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self._error:
            raise self._error

    async def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self.response, self.urls = response, []

    def get(self, url):
        self.urls.append(url)
        return self.response


class TestGetJson:
    async def test_reads_the_json_of_an_api_path(self):
        session = _Session(_Response({"ok": True}))
        assert await el._get_json(session, "/voices") == {"ok": True}  # type: ignore[arg-type]
        assert session.urls == ["https://api.elevenlabs.io/v1/voices"]

    async def test_an_http_error_is_raised(self):
        session = _Session(_Response(None, http_error(402)))
        with pytest.raises(aiohttp.ClientResponseError):
            await el._get_json(session, "/voices")  # type: ignore[arg-type]
