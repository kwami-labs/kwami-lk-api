"""`/voices` and `/languages` — the catalog surface the app's pickers read.

Both routers are the same shape: a grouped "all" view, a per-provider view that
404s with the available list, and a conversion layer between the service models
and the response models. The conversions are where a field silently goes
missing, so each one is asserted field by field.
"""

from __future__ import annotations

import pytest

from src.api.routes import languages as languages_route
from src.api.routes import voices as voices_route
from src.services.languages import Language, ProviderLanguages
from src.services.voices import Voice, VoiceProvider

pytestmark = pytest.mark.anyio


# -- conversions -------------------------------------------------------------


class TestVoiceConversion:
    def test_every_field_survives(self):
        r = voices_route._convert_voice(
            Voice(
                id="v1",
                name="One",
                category="Female",
                gender="female",
                language="en",
                description="warm",
            )
        )
        assert (r.id, r.name, r.category, r.gender, r.language, r.description) == (
            "v1",
            "One",
            "Female",
            "female",
            "en",
            "warm",
        )

    def test_absent_optionals_stay_absent(self):
        r = voices_route._convert_voice(Voice(id="v", name="V"))
        assert r.category is None and r.gender is None
        assert r.language is None and r.description is None

    def test_a_provider_carries_its_source_and_a_derived_count(self):
        r = voices_route._convert_provider(
            VoiceProvider(
                provider="p",
                source="sdk",
                voices=[Voice(id="a", name="A"), Voice(id="b", name="B")],
            )
        )
        assert r.provider == "p"
        assert r.source == "sdk"
        assert r.count == 2
        assert [v.id for v in r.voices] == ["a", "b"]

    def test_an_empty_provider_counts_zero(self):
        assert voices_route._convert_provider(VoiceProvider(provider="p", voices=[])).count == 0


class TestLanguageConversion:
    def test_every_field_survives(self):
        r = languages_route._convert_language(
            Language(code="sw", name="Swahili", native_name="Kiswahili", region="EA")
        )
        assert (r.code, r.name, r.native_name, r.region) == ("sw", "Swahili", "Kiswahili", "EA")

    def test_absent_optionals_stay_absent(self):
        r = languages_route._convert_language(Language(code="zz", name="ZZ"))
        assert r.native_name is None and r.region is None

    def test_a_provider_carries_its_source_and_a_derived_count(self):
        r = languages_route._convert_provider(
            ProviderLanguages(
                provider="p",
                source="yaml",
                languages=[Language(code="en", name="English")],
            )
        )
        assert r.provider == "p" and r.source == "yaml" and r.count == 1


# -- /voices -----------------------------------------------------------------


class TestTtsVoiceRoutes:
    async def test_the_grouped_view_totals_across_providers(self, client, monkeypatch):
        monkeypatch.setattr(
            voices_route,
            "get_tts_voices",
            lambda: {
                "openai": VoiceProvider(
                    provider="openai",
                    source="sdk",
                    voices=[Voice(id="a", name="A"), Voice(id="b", name="B")],
                ),
                "cartesia": VoiceProvider(
                    provider="cartesia", source="yaml", voices=[Voice(id="c", name="C")]
                ),
            },
        )
        r = await client.get("/voices/tts")
        assert r.status_code == 200
        body = r.json()
        assert body["total_providers"] == 2
        assert body["total_voices"] == 3
        assert body["providers"]["openai"]["source"] == "sdk"
        assert body["providers"]["cartesia"]["count"] == 1

    async def test_an_empty_catalog_is_an_empty_response_not_an_error(self, client, monkeypatch):
        monkeypatch.setattr(voices_route, "get_tts_voices", dict)
        r = await client.get("/voices/tts")
        assert r.status_code == 200
        assert r.json() == {"providers": {}, "total_voices": 0, "total_providers": 0}

    async def test_the_real_catalog_is_served(self, client):
        r = await client.get("/voices/tts")
        assert r.status_code == 200
        assert r.json()["total_voices"] > 0

    async def test_a_known_provider(self, client):
        r = await client.get("/voices/tts/openai")
        assert r.status_code == 200
        assert r.json()["provider"] == "openai"
        assert r.json()["count"] > 0

    async def test_an_unknown_provider_404s_and_lists_what_exists(self, client):
        r = await client.get("/voices/tts/nope")
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert "nope" in detail
        assert "openai" in detail, "the error should say what is available"


class TestRealtimeVoiceRoutes:
    async def test_the_grouped_view_totals_across_providers(self, client, monkeypatch):
        monkeypatch.setattr(
            voices_route,
            "get_realtime_voices",
            lambda: {
                "gemini": VoiceProvider(
                    provider="gemini", source="sdk", voices=[Voice(id="g", name="G")]
                )
            },
        )
        body = (await client.get("/voices/realtime")).json()
        assert body["total_providers"] == 1 and body["total_voices"] == 1

    async def test_an_empty_catalog_is_an_empty_response(self, client, monkeypatch):
        monkeypatch.setattr(voices_route, "get_realtime_voices", dict)
        assert (await client.get("/voices/realtime")).json()["total_providers"] == 0

    async def test_the_real_catalog_is_served(self, client):
        assert (await client.get("/voices/realtime")).json()["total_voices"] > 0

    async def test_a_known_provider(self, client):
        r = await client.get("/voices/realtime/gemini")
        assert r.status_code == 200 and r.json()["provider"] == "gemini"

    async def test_an_unknown_provider_404s_and_lists_what_exists(self, client):
        r = await client.get("/voices/realtime/nope")
        assert r.status_code == 404
        assert "gemini" in r.json()["detail"]


# -- /languages --------------------------------------------------------------


class TestLanguageReferenceRoute:
    async def test_it_lists_every_known_language(self, client):
        from src.services.languages import LANGUAGE_NAMES

        body = (await client.get("/languages")).json()
        assert body["count"] == len(LANGUAGE_NAMES)
        assert len(body["languages"]) == body["count"]

    async def test_entries_carry_their_native_name(self, client):
        body = (await client.get("/languages")).json()
        swahili = next(lang for lang in body["languages"] if lang["code"] == "sw")
        assert swahili["native_name"] == "Kiswahili"


class TestSttLanguageRoutes:
    async def test_the_total_counts_distinct_codes_not_the_sum(self, client, monkeypatch):
        """Two providers both supporting `en` is one language, not two."""
        monkeypatch.setattr(
            languages_route,
            "get_stt_languages",
            lambda: {
                "a": ProviderLanguages(
                    provider="a",
                    source="sdk",
                    languages=[
                        Language(code="en", name="English"),
                        Language(code="fr", name="French"),
                    ],
                ),
                "b": ProviderLanguages(
                    provider="b", source="yaml", languages=[Language(code="en", name="English")]
                ),
            },
        )
        body = (await client.get("/languages/stt")).json()
        assert body["total_providers"] == 2
        assert body["total_languages"] == 2, "en is shared"
        assert body["providers"]["a"]["count"] == 2

    async def test_an_empty_catalog_is_an_empty_response(self, client, monkeypatch):
        monkeypatch.setattr(languages_route, "get_stt_languages", dict)
        body = (await client.get("/languages/stt")).json()
        assert body == {"providers": {}, "total_languages": 0, "total_providers": 0}

    async def test_the_real_catalog_is_served(self, client):
        assert (await client.get("/languages/stt")).json()["total_languages"] > 0

    async def test_a_known_provider(self, client):
        r = await client.get("/languages/stt/deepgram")
        assert r.status_code == 200 and r.json()["provider"] == "deepgram"

    async def test_an_unknown_provider_404s_and_lists_what_exists(self, client):
        r = await client.get("/languages/stt/nope")
        assert r.status_code == 404
        assert "deepgram" in r.json()["detail"]


class TestTtsLanguageRoutes:
    async def test_the_grouped_view(self, client, monkeypatch):
        monkeypatch.setattr(
            languages_route,
            "get_tts_languages",
            lambda: {
                "cartesia": ProviderLanguages(
                    provider="cartesia",
                    source="sdk",
                    languages=[Language(code="en", name="English")],
                )
            },
        )
        body = (await client.get("/languages/tts")).json()
        assert body["total_providers"] == 1 and body["total_languages"] == 1

    async def test_an_empty_catalog_is_an_empty_response(self, client, monkeypatch):
        monkeypatch.setattr(languages_route, "get_tts_languages", dict)
        assert (await client.get("/languages/tts")).json()["total_providers"] == 0

    async def test_the_real_catalog_is_served(self, client):
        assert (await client.get("/languages/tts")).json()["total_languages"] > 0

    async def test_a_known_provider(self, client):
        r = await client.get("/languages/tts/cartesia")
        assert r.status_code == 200 and r.json()["provider"] == "cartesia"

    async def test_an_unknown_provider_404s(self, client):
        assert (await client.get("/languages/tts/nope")).status_code == 404


class TestRealtimeLanguageRoutes:
    async def test_the_grouped_view(self, client, monkeypatch):
        monkeypatch.setattr(
            languages_route,
            "get_realtime_languages",
            lambda: {
                "gemini": ProviderLanguages(
                    provider="gemini",
                    source="yaml",
                    languages=[Language(code="en", name="English")],
                )
            },
        )
        body = (await client.get("/languages/realtime")).json()
        assert body["total_providers"] == 1 and body["total_languages"] == 1

    async def test_an_empty_catalog_is_an_empty_response(self, client, monkeypatch):
        monkeypatch.setattr(languages_route, "get_realtime_languages", dict)
        assert (await client.get("/languages/realtime")).json()["total_providers"] == 0

    async def test_a_known_provider(self, client, monkeypatch):
        monkeypatch.setattr(
            languages_route,
            "get_realtime_languages_by_provider",
            lambda p: ProviderLanguages(
                provider=p, source="yaml", languages=[Language(code="en", name="English")]
            ),
        )
        r = await client.get("/languages/realtime/openai")
        assert r.status_code == 200 and r.json()["provider"] == "openai"

    async def test_an_unknown_provider_404s_and_lists_what_exists(self, client, monkeypatch):
        monkeypatch.setattr(languages_route, "get_realtime_languages_by_provider", lambda p: None)
        monkeypatch.setattr(
            languages_route,
            "get_realtime_languages",
            lambda: {"gemini": ProviderLanguages(provider="gemini", source="yaml", languages=[])},
        )
        r = await client.get("/languages/realtime/nope")
        assert r.status_code == 404
        assert "gemini" in r.json()["detail"]
