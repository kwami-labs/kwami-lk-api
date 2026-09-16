"""Voices API endpoints.

Provides endpoints for retrieving available TTS and Realtime voices
grouped by provider. Uses a hybrid approach:
- SDK-extracted voices for OpenAI, Google Gemini, Rime
- YAML-defined voices for Cartesia, Deepgram, ElevenLabs, Google Cloud TTS
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


class ProviderVoicesResponse(BaseModel):
    """Voices for a single provider."""

    provider: str = Field(description="Provider identifier")
    voices: list[VoiceResponse] = Field(description="List of available voices")
    source: Literal["sdk", "yaml"] = Field(
        description="Data source (sdk = extracted from LiveKit SDK)"
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
    - **elevenlabs**: From YAML config (20 voices)
    - **deepgram**: From YAML config (12 voices)
    - **google**: From YAML config (11 voices)

    Each voice includes id, name, category, gender, and language information.
    """
    providers_data = get_tts_voices()

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
    - openai, rime (SDK-extracted)
    - cartesia, elevenlabs, deepgram, google (YAML-defined)
    """
    provider_data = get_tts_voices_by_provider(provider)

    if not provider_data:
        available = list(get_tts_voices().keys())
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' not found. Available: {available}"
        )

    return _convert_provider(provider_data)


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
