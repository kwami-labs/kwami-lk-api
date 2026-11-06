"""`src.services.voice_preview` and `GET /voices/preview`.

What matters: only voices of the catalogue go out, the same sample is paid for once,
a key is the deployment's or the user's own, and no provider's text comes back.
"""

from __future__ import annotations

import asyncio
import base64
import json
import wave
from io import BytesIO

import aiohttp
import httpx
import pytest
import respx
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.services import provider_keys_service
from src.services import voice_preview as vp
from src.services.provider_keys_service import ProviderKeysNotConfiguredError
from src.services.voices import Voice

pytestmark = pytest.mark.anyio

USER = "user-1"
CARTESIA = "d3a5be1f-f963-4c85-bfcc-c3b20903d967"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    vp.reset()
    monkeypatch.setattr(vp.settings, "openai_api_key", None, raising=False)
    monkeypatch.setattr(vp.settings, "google_api_key", None, raising=False)
    monkeypatch.setattr(vp.settings, "livekit_api_key", "key", raising=False)
    monkeypatch.setattr(vp.settings, "livekit_api_secret", "s" * 40, raising=False)
    yield
    vp.reset()


def _voice(voice_id="v", language="en", category=None) -> Voice:
    return Voice(id=voice_id, name=voice_id, language=language, category=category)


class TestWhichLanguage:
    def test_the_voice_s_own_when_it_has_a_sentence(self):
        assert vp._language(_voice(language="fr-FR"), "es") == "fr"

    def test_the_user_s_for_a_voice_of_any_language(self):
        assert vp._language(_voice(language="multilingual"), "es-ES") == "es"

    def test_english_when_neither_has_one(self):
        assert vp._language(_voice(language="ar-SA"), "ko") == "en"


class TestOnlyTheCatalogue:
    async def test_a_voice_it_does_not_list_never_goes_out(self):
        with pytest.raises(vp.UnknownVoiceError):
            await vp.preview(USER, "cartesia", "../other", "es")

    async def test_a_provider_with_samples_of_its_own_has_none_here(self):
        voice = _voice()
        with pytest.raises(vp.PreviewNotAvailableError):
            await vp._speak(USER, "deepgram", voice, "en")

    def test_a_realtime_voice_counts(self):
        assert vp._find("openai", "marin").id == "marin"


class TestPaidForOnce:
    async def test_the_second_ask_is_kept_and_two_at_once_are_one(self, monkeypatch):
        calls = 0

        async def speak(*_):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return vp.Sample(b"audio")

        monkeypatch.setattr(vp, "_speak", speak)
        first, second = await asyncio.gather(
            vp.preview(USER, "cartesia", CARTESIA, "es"),
            vp.preview("user-2", "cartesia", CARTESIA, "es"),
        )
        assert first.audio == second.audio == b"audio"
        await vp.preview(USER, "cartesia", CARTESIA, "es")
        assert calls == 1

    async def test_the_oldest_makes_room(self, monkeypatch):
        async def speak(*_):
            return vp.Sample(b"audio")

        monkeypatch.setattr(vp, "_speak", speak)
        monkeypatch.setattr(vp, "CACHE_ENTRIES", 1)
        await vp.preview(USER, "openai", "alloy", "es")
        await vp.preview(USER, "openai", "marin", "es")
        assert [key[1] for key in vp._cache] == ["marin"]

    async def test_a_silent_answer_is_not_kept(self, monkeypatch):
        async def speak(*_):
            return vp.Sample(b"")

        monkeypatch.setattr(vp, "_speak", speak)
        with pytest.raises(vp.PreviewFailedError):
            await vp.preview(USER, "openai", "alloy", "es")
        assert not vp._cache

    async def test_a_network_failure_says_nothing_of_it(self, monkeypatch):
        async def speak(*_):
            raise aiohttp.ClientError("secret detail")

        monkeypatch.setattr(vp, "_speak", speak)
        with pytest.raises(vp.PreviewFailedError) as caught:
            await vp.preview(USER, "openai", "alloy", "es")
        assert "secret" not in str(caught.value)


class TestTheGateway:
    @pytest.fixture
    async def gateway(self, monkeypatch):
        """A stand-in that answers as LiveKit Inference does, and keeps what it was sent."""
        seen = {"messages": [], "auth": None, "model": None, "answer": "audio"}

        async def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            seen["model"] = request.query.get("model")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                seen["messages"].append(json.loads(message.data))
                if seen["messages"][-1]["type"] != "session.flush":
                    continue
                if seen["answer"] == "error":
                    await ws.send_str(json.dumps({"type": "error", "code": "bad_voice"}))
                elif seen["answer"] == "closes":
                    await ws.close()
                elif seen["answer"] == "binary":
                    await ws.send_bytes(b"not json")
                else:
                    audio = base64.b64encode(b"\x01\x02").decode()
                    await ws.send_str(json.dumps({"type": "other"}))
                    await ws.send_str(json.dumps({"type": "output_audio", "audio": audio}))
                    await ws.send_str(json.dumps({"type": "done"}))
            return ws

        app = web.Application()
        app.router.add_get("/tts", handler)
        server = TestServer(app)
        await server.start_server()
        monkeypatch.setattr(vp, "GATEWAY_URL", str(server.make_url("/tts")))
        yield seen
        await server.close()

    async def test_cartesia_speaks_the_fixed_sentence_in_the_voice_s_language(self, gateway):
        sample = await vp._cartesia(_voice(CARTESIA), "es", vp.SENTENCES["es"])
        create, text, flush = gateway["messages"]
        assert create["model"] == gateway["model"] == "cartesia/sonic-3"
        assert create["voice"] == CARTESIA and create["language"] == "es"
        assert text["transcript"].strip() == vp.SENTENCES["es"]
        assert flush == {"type": "session.flush"}
        assert gateway["auth"].startswith("Bearer ")
        with wave.open(BytesIO(sample.audio)) as file:
            assert file.getframerate() == vp.SAMPLE_RATE and file.getnchannels() == 1
            assert file.readframes(1) == b"\x01\x02"

    async def test_a_voice_designed_in_livekit_speaks_on_sonic_3_5(self, gateway):
        """LiveKit designs its "v_" voices on Sonic 3.5, not on Cartesia's Sonic 3."""
        await vp._cartesia(_voice("v_ZMPWZvfoRpsC", language="es-ES"), "es", "x")
        create = gateway["messages"][0]
        assert create["model"] == gateway["model"] == "cartesia/sonic-3.5"
        assert create["voice"] == "v_ZMPWZvfoRpsC"

    async def test_arcana_reads_the_language_from_the_text(self, gateway):
        await vp._rime(_voice("luna", category="arcana"), "es", "x")
        assert gateway["model"] == "rime/arcana"
        assert "language" not in gateway["messages"][0]

    async def test_mist_is_told_the_language(self, gateway):
        await vp._rime(_voice("astra", category="mistv2"), "es", "x")
        assert gateway["model"] == "rime/mistv2"
        assert gateway["messages"][0]["language"] == "spa"

    async def test_a_refusal_is_a_failure(self, gateway):
        gateway["answer"] = "error"
        with pytest.raises(vp.PreviewFailedError):
            await vp._cartesia(_voice(), "en", "x")

    @pytest.mark.parametrize("answer", ["closes", "binary"])
    async def test_a_line_that_ends_without_done_is_a_failure(self, gateway, answer):
        gateway["answer"] = answer
        with pytest.raises(vp.PreviewFailedError):
            await vp._cartesia(_voice(), "en", "x")

    async def test_rime_and_cartesia_are_routed_there(self, gateway):
        await vp._speak(USER, "cartesia", _voice(), "en")
        await vp._speak(USER, "rime", _voice(category="arcana"), "en")
        assert gateway["model"] == "rime/arcana"


class TestWhoseKey:
    @respx.mock
    async def test_the_deployment_s_first(self, monkeypatch):
        monkeypatch.setattr(vp.settings, "openai_api_key", "deploy-key", raising=False)
        route = respx.post(vp.OPENAI_URL).mock(return_value=httpx.Response(200, content=b""))
        sample = await vp._speak(USER, "openai", _voice("alloy"), "es")
        assert sample.audio.startswith(b"RIFF")
        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer deploy-key"
        body = json.loads(request.content)
        assert body["voice"] == "alloy" and body["input"] == vp.SENTENCES["es"]

    @respx.mock
    async def test_else_the_user_s_own(self, monkeypatch):
        async def key_of(user_id, provider):
            assert (user_id, provider) == (USER, "google")
            return "their-key"

        monkeypatch.setattr(provider_keys_service, "key_of", key_of)
        pcm = base64.b64encode(b"\x01\x02").decode()
        route = respx.post(vp.GEMINI_URL).mock(
            return_value=httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": pcm}}]}}]}
            )
        )
        sample = await vp._speak(USER, "gemini", _voice("Charon"), "es")
        assert route.calls.last.request.headers["x-goog-api-key"] == "their-key"
        assert sample.audio.startswith(b"RIFF")
        voice = json.loads(route.calls.last.request.content)["generationConfig"]["speechConfig"]
        assert voice["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Charon"

    @pytest.mark.parametrize("found", ["none", "off"])
    async def test_without_any_there_is_no_sample(self, monkeypatch, found):
        async def key_of(*_):
            if found == "off":
                raise ProviderKeysNotConfiguredError("off")

        monkeypatch.setattr(provider_keys_service, "key_of", key_of)
        with pytest.raises(vp.PreviewNotAvailableError):
            await vp._speak(USER, "openai", _voice("alloy"), "es")

    @respx.mock
    async def test_a_refusal_keeps_the_provider_s_text_out(self, monkeypatch):
        monkeypatch.setattr(vp.settings, "openai_api_key", "k", raising=False)
        monkeypatch.setattr(vp.settings, "google_api_key", "k", raising=False)
        respx.post(vp.OPENAI_URL).mock(return_value=httpx.Response(401, text="secret detail"))
        respx.post(vp.GEMINI_URL).mock(return_value=httpx.Response(429, text="secret detail"))
        for provider in ("openai", "gemini"):
            with pytest.raises(vp.PreviewFailedError) as caught:
                await vp._speak(USER, provider, _voice("x"), "en")
            assert "secret" not in str(caught.value)

    @respx.mock
    async def test_gemini_with_no_audio_in_it_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(vp.settings, "google_api_key", "k", raising=False)
        respx.post(vp.GEMINI_URL).mock(return_value=httpx.Response(200, json={"candidates": []}))
        with pytest.raises(vp.PreviewFailedError):
            await vp._speak(USER, "google", _voice("x"), "en")


class TestTheRoute:
    async def test_needs_a_login(self, client):
        response = await client.get("/voices/preview", params={"provider": "openai", "voice": "a"})
        assert response.status_code == 401

    async def test_gives_the_audio(self, tenant_client, monkeypatch):
        async def preview(*_):
            return vp.Sample(b"RIFF")

        monkeypatch.setattr(vp, "preview", preview)
        response = await tenant_client.get(
            "/voices/preview", params={"provider": "openai", "voice": "alloy", "lang": "es"}
        )
        assert response.status_code == 200 and response.content == b"RIFF"
        assert response.headers["content-type"] == "audio/wav"

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (vp.UnknownVoiceError("x"), 404),
            (vp.PreviewNotAvailableError("x"), 503),
            (vp.PreviewFailedError("x"), 502),
        ],
    )
    async def test_says_what_went_wrong(self, tenant_client, monkeypatch, error, status):
        async def preview(*_):
            raise error

        monkeypatch.setattr(vp, "preview", preview)
        response = await tenant_client.get(
            "/voices/preview", params={"provider": "openai", "voice": "alloy"}
        )
        assert response.status_code == status
