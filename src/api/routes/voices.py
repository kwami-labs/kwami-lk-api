"""Voices API endpoints.

Provides endpoints for retrieving available TTS and Realtime voices
grouped by provider. Uses a hybrid approach:
- SDK-extracted voices for OpenAI, Google Gemini, Rime
- YAML-defined voices for Cartesia, Deepgram, ElevenLabs, Google Cloud TTS
- ElevenLabs from the account itself when the API has its key
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.api.ratelimit import VOICE_PREVIEW_LIMIT, limiter
from src.core.security import AuthUser
from src.services import elevenlabs_voices, voice_preview
from src.services.voices import (
    Voice,
    VoiceProvider,
    get_realtime_voices,
    get_realtime_voices_by_provider,
    get_tts_voices,
    get_tts_voices_by_provider,
)

logger = logging.getLogger("kwami-api.voices")
router = APIRouter()


# =============================================================================
# Response Models
# =============================================================================


class VoiceResponse(BaseModel):
    """Single voice in response."""

    id: str = Field(description="Unique voice identifier (provider-specific)")
    name: str = Field(description="Display name for the voice")
    category: str | None = Field(
        default=None, description="Voice category (e.g., 'Female EN', 'Male')"
    )
    gender: Literal["male", "female", "neutral"] | None = Field(
        default=None, description="Voice gender"
    )
    language: str | None = Field(default=None, description="Primary language code")
    description: str | None = Field(default=None, description="Additional description")
    preview_url: str | None = Field(default=None, description="A sample to listen to")


class ProviderVoicesResponse(BaseModel):
    """Voices for a single provider."""

    provider: str = Field(description="Provider identifier")
    voices: list[VoiceResponse] = Field(description="List of available voices")
    source: Literal["sdk", "yaml", "live"] = Field(
        description="Data source (sdk = LiveKit SDK, live = the provider's own account)"
    )
    count: int = Field(description="Number of voices")


class AllVoicesResponse(BaseModel):
    """Response containing all voices grouped by provider."""

    providers: dict[str, ProviderVoicesResponse] = Field(description="Voices grouped by provider")
    total_voices: int = Field(description="Total number of voices across all providers")
    total_providers: int = Field(description="Number of providers")


# =============================================================================
# Helper Functions
# =============================================================================


def _convert_voice(voice: Voice) -> VoiceResponse:
    """Convert internal Voice model to response model."""
    return VoiceResponse(
        id=voice.id,
        name=voice.name,
        category=voice.category,
        gender=voice.gender,
        language=voice.language,
        description=voice.description,
        preview_url=voice.preview_url,
    )


def _convert_provider(provider: VoiceProvider) -> ProviderVoicesResponse:
    """Convert internal VoiceProvider model to response model."""
    return ProviderVoicesResponse(
        provider=provider.provider,
        voices=[_convert_voice(v) for v in provider.voices],
        source=provider.source,
        count=len(provider.voices),
    )


# =============================================================================
# TTS Endpoints
# =============================================================================


@router.get("/tts", response_model=AllVoicesResponse)
async def get_all_tts_voices():
    """
    Get all available TTS voices grouped by provider.

    Returns voices from multiple providers:
    - **openai**: Extracted from LiveKit SDK (10 voices)
    - **rime**: Extracted from LiveKit SDK (8 voices)
    - **cartesia**: From YAML config (24 voices)
    - **gemini**: Gemini TTS, the 30 Gemini Live voices (SDK)
    - **elevenlabs**: The account's own voices with a key, else YAML config
    - **deepgram**: From YAML config (12 voices)
    - **google**: Google Cloud TTS, from YAML config (11 voices)

    Each voice includes id, name, category, gender, and language information.
    """
    providers_data = get_tts_voices(await elevenlabs_voices.account_voices())

    providers = {}
    total_voices = 0

    for provider_name, provider in providers_data.items():
        converted = _convert_provider(provider)
        providers[provider_name] = converted
        total_voices += converted.count

    return AllVoicesResponse(
        providers=providers,
        total_voices=total_voices,
        total_providers=len(providers),
    )


@router.get("/tts/{provider}", response_model=ProviderVoicesResponse)
async def get_tts_voices_for_provider(provider: str):
    """
    Get TTS voices for a specific provider.

    Valid providers:
    - openai, rime, gemini (SDK-extracted)
    - elevenlabs (the account's own voices with a key, else YAML)
    - cartesia, deepgram, google (YAML-defined)
    """
    live = await elevenlabs_voices.account_voices() if provider == "elevenlabs" else None
    provider_data = get_tts_voices_by_provider(provider, live)

    if not provider_data:
        available = list(get_tts_voices().keys())
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' not found. Available: {available}"
        )

    return _convert_provider(provider_data)


# =============================================================================
# Hearing a voice
# =============================================================================


@router.get("/preview")
@limiter.limit(VOICE_PREVIEW_LIMIT)
async def voice_sample(
    request: Request,
    user: Annotated[AuthUser, Depends(require_auth)],
    provider: Annotated[str, Query(max_length=32)],
    voice: Annotated[str, Query(max_length=128)],
    lang: Annotated[str, Query(max_length=16)] = "en",
) -> Response:
    """A short sample of a voice that publishes none of its own."""
    try:
        sample = await voice_preview.preview(user.id, provider, voice, lang)
    except voice_preview.UnknownVoiceError as exc:
        raise HTTPException(status_code=404, detail="Not a voice of this provider") from exc
    except voice_preview.PreviewNotAvailableError as exc:
        raise HTTPException(status_code=503, detail="No sample here for this provider") from exc
    except voice_preview.PreviewFailedError as exc:
        raise HTTPException(status_code=502, detail="The provider gave no sample") from exc
    return Response(
        content=sample.audio,
        media_type=sample.media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


# =============================================================================
# Realtime Endpoints
# =============================================================================


@router.get("/realtime", response_model=AllVoicesResponse)
async def get_all_realtime_voices():
    """
    Get all available Realtime voices grouped by provider.

    Returns voices from multimodal realtime providers:
    - **openai**: Extracted from LiveKit SDK (10 voices - same as TTS)
    - **gemini**: Extracted from LiveKit SDK (30 voices)

    Realtime voices are used for bidirectional voice conversations.
    """
    providers_data = get_realtime_voices()

    providers = {}
    total_voices = 0

    for provider_name, provider in providers_data.items():
        converted = _convert_provider(provider)
        providers[provider_name] = converted
        total_voices += converted.count

    return AllVoicesResponse(
        providers=providers,
        total_voices=total_voices,
        total_providers=len(providers),
    )


@router.get("/realtime/{provider}", response_model=ProviderVoicesResponse)
async def get_realtime_voices_for_provider(provider: str):
    """
    Get Realtime voices for a specific provider.

    Valid providers:
    - openai (SDK-extracted, same voices as TTS)
    - gemini (SDK-extracted, 30 Gemini Live voices)
    """
    provider_data = get_realtime_voices_by_provider(provider)

    if not provider_data:
        available = list(get_realtime_voices().keys())
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' not found. Available: {available}"
        )

    return _convert_provider(provider_data)
