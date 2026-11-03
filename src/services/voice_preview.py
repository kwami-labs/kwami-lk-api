"""Hearing a voice before choosing it.

Deepgram and ElevenLabs publish a sample of each voice; Cartesia, Rime, OpenAI and
Gemini do not, so 1,250 voices of the picker could only be chosen blind. This speaks
a fixed sentence with one of them, once: the answer is kept for everybody who asks.

Only voices of the catalogue and only the sentences below ever go out, so the route
cannot be turned into a free text-to-speech. Cartesia and Rime go through LiveKit
Inference, as the agent's Rime does, and need no key of their own. OpenAI and Gemini
take the deployment's key or, without one, the key the user connected.

Failures carry no upstream text: an answer from a provider is not ours to pass on.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import wave
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import aiohttp
import httpx
from livekit import api

from src.core.config import settings
from src.services import provider_keys_service
from src.services.provider_keys_service import ProviderKeysNotConfiguredError
from src.services.voices import (
    Voice,
    get_realtime_voices_by_provider,
    get_tts_voices_by_provider,
)

logger = logging.getLogger("kwami-api.voices.preview")

GATEWAY_URL = "wss://agent-gateway.livekit.cloud/v1/tts"
OPENAI_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_MODEL = "gpt-4o-mini-tts"
GEMINI_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

SAMPLE_RATE = 24000
TIMEOUT_SECONDS = 20.0
#: A sentence of about five seconds is some 240 KB, so this stays under 100 MB.
CACHE_ENTRIES = 400

SENTENCES = {
    "es": "Hola. Así sonaría cada día al hablar contigo, tanto para darte una noticia como para contarte un chiste.",
    "en": "Hello. This is how I would sound every day, whether I am giving you the news or telling you a joke.",
    "fr": "Bonjour. Voici comment je sonnerais chaque jour, pour vous donner une nouvelle ou vous raconter une blague.",
    "de": "Hallo. So würde ich jeden Tag klingen, ob ich dir eine Nachricht überbringe oder einen Witz erzähle.",
    "it": "Ciao. Ecco come suonerei ogni giorno, sia per darti una notizia sia per raccontarti una barzelletta.",
    "pt": "Olá. É assim que eu soaria todos os dias, para te dar uma notícia ou para te contar uma piada.",
}
#: What the gateway calls a language, on Rime's voices that are not Arcana's.
RIME_LANGUAGES = {"es": "spa", "en": "eng", "fr": "fra", "de": "ger"}


class UnknownVoiceError(LookupError):
    """Not a voice of this provider's catalogue."""


class PreviewNotAvailableError(RuntimeError):
    """This provider cannot speak a sample here: it publishes its own, or has no key."""


class PreviewFailedError(RuntimeError):
    """The provider did not give the sample. Carries no upstream text."""


@dataclass(frozen=True)
class Sample:
    audio: bytes
    media_type: str = "audio/wav"


_cache: OrderedDict[tuple[str, str, str], Sample] = OrderedDict()
_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def reset() -> None:
    _cache.clear()
    _locks.clear()


def _find(provider: str, voice_id: str) -> Voice:
    catalogues = (get_tts_voices_by_provider(provider), get_realtime_voices_by_provider(provider))
    for catalogue in catalogues:
        for voice in catalogue.voices if catalogue else []:
            if voice.id == voice_id:
                return voice
    raise UnknownVoiceError(voice_id)


def _language(voice: Voice, wanted: str) -> str:
    """The voice's own language when it has one of the sentences, else the user's."""
    own = (voice.language or "").split("-")[0].lower()
    if own in SENTENCES:
        return own
    base = wanted.split("-")[0].lower()
    return base if base in SENTENCES else "en"


def _wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(SAMPLE_RATE)
        file.writeframes(pcm)
    return out.getvalue()


def _gateway_token() -> str:
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("voice-preview")
        .with_inference_grants(api.access_token.InferenceGrants(perform=True))
        .with_ttl(timedelta(seconds=TIMEOUT_SECONDS * 2))
        .to_jwt()
    )


async def _gateway(model: str, voice: str, language: str | None, text: str) -> bytes:
    create: dict[str, object] = {
        "type": "session.create",
        "sample_rate": str(SAMPLE_RATE),
        "encoding": "pcm_s16le",
        "model": model,
        "voice": voice,
        "extra": {},
    }
    if language:
        create["language"] = language
    pcm = bytearray()
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.ws_connect(
            f"{GATEWAY_URL}?{urlencode({'model': model})}",
            headers={"Authorization": f"Bearer {_gateway_token()}"},
        ) as ws,
    ):
        await ws.send_str(json.dumps(create))
        await ws.send_str(json.dumps({"type": "input_transcript", "transcript": text + " "}))
        await ws.send_str(json.dumps({"type": "session.flush"}))
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(message.data)
            if data.get("type") == "output_audio":
                pcm += base64.b64decode(data["audio"])
            elif data.get("type") == "done":
                return bytes(pcm)
            elif data.get("type") == "error":
                logger.warning("The gateway refused a sample of %s: %s", model, data.get("code"))
                raise PreviewFailedError(model)
    raise PreviewFailedError(model)


async def _cartesia(voice: Voice, language: str, text: str) -> Sample:
    return Sample(_wav(await _gateway("cartesia/sonic-3", voice.id, language, text)))


async def _rime(voice: Voice, language: str, text: str) -> Sample:
    # Arcana reads the language from the text; Mist is told it.
    if voice.category == "arcana":
        return Sample(_wav(await _gateway("rime/arcana", voice.id, None, text)))
    code = RIME_LANGUAGES.get(language, "eng")
    return Sample(_wav(await _gateway("rime/mistv2", voice.id, code, text)))


async def _key(user_id: str, provider: str, own: str | None) -> str:
    """The deployment's key, or the one this user connected."""
    if own:
        return own
    try:
        key = await provider_keys_service.key_of(user_id, provider)
    except ProviderKeysNotConfiguredError:
        key = None
    if not key:
        raise PreviewNotAvailableError(provider)
    return key


async def _openai(key: str, voice: Voice, text: str) -> Sample:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": OPENAI_MODEL,
                "voice": voice.id,
                "input": text,
                "response_format": "pcm",
            },
        )
    if response.status_code >= 300:
        logger.warning("OpenAI answered %s to a sample", response.status_code)
        raise PreviewFailedError("openai")
    # Plain samples: its WAV is streamed, and its header says it lasts a day.
    return Sample(_wav(response.content))


async def _gemini(key: str, voice: Voice, text: str) -> Sample:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            GEMINI_URL,
            headers={"x-goog-api-key": key},
            json={
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice.id}}
                    },
                },
            },
        )
    if response.status_code >= 300:
        logger.warning("Gemini answered %s to a sample", response.status_code)
        raise PreviewFailedError("gemini")
    try:
        part = response.json()["candidates"][0]["content"]["parts"][0]
        return Sample(_wav(base64.b64decode(part["inlineData"]["data"])))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise PreviewFailedError("gemini") from exc


async def _speak(user_id: str, provider: str, voice: Voice, language: str) -> Sample:
    text = SENTENCES[language]
    speakers: dict[str, Callable[[], Awaitable[Sample]]] = {
        "cartesia": lambda: _cartesia(voice, language, text),
        "rime": lambda: _rime(voice, language, text),
    }
    if provider in speakers:
        return await speakers[provider]()
    if provider == "openai":
        return await _openai(await _key(user_id, "openai", settings.openai_api_key), voice, text)
    if provider in ("gemini", "google"):
        return await _gemini(await _key(user_id, "google", settings.google_api_key), voice, text)
    raise PreviewNotAvailableError(provider)


async def preview(user_id: str, provider: str, voice_id: str, wanted_language: str) -> Sample:
    """The sample of a voice of the catalogue, made once and kept for everybody."""
    voice = _find(provider, voice_id)
    language = _language(voice, wanted_language)
    key = (provider, voice_id, language)
    # Two people, or two clicks, on the same voice pay for one sample.
    async with _locks.setdefault(key, asyncio.Lock()):
        cached = _cache.get(key)
        if cached:
            _cache.move_to_end(key)
            return cached
        try:
            sample = await _speak(user_id, provider, voice, language)
        except (aiohttp.ClientError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            logger.warning("No sample: %s", type(exc).__name__)
            raise PreviewFailedError(provider) from exc
        if not sample.audio:
            raise PreviewFailedError(provider)
        _cache[key] = sample
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)
        return sample
