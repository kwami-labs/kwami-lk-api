"""Languages API endpoints.

Provides endpoints for retrieving supported languages for STT, TTS,
and Realtime models grouped by provider.
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.services.languages import (
    Language,
    ProviderLanguages,
    get_all_languages,
    get_realtime_languages,
    get_realtime_languages_by_provider,
    get_stt_languages,
    get_stt_languages_by_provider,
    get_tts_languages,
    get_tts_languages_by_provider,
)

logger = logging.getLogger("kwami-api.languages")
router = APIRouter()


# =============================================================================
# Response Models
# =============================================================================


class LanguageResponse(BaseModel):
    """Single language in response."""

    code: str = Field(description="ISO language code (e.g., 'en', 'en-US')")
    name: str = Field(description="Human-readable name (e.g., 'English')")
    native_name: str | None = Field(default=None, description="Name in native language")
    region: str | None = Field(default=None, description="Region code if applicable")


class ProviderLanguagesResponse(BaseModel):
    """Languages for a single provider."""

    provider: str = Field(description="Provider identifier")
    languages: list[LanguageResponse] = Field(description="List of supported languages")
    source: Literal["sdk", "yaml"] = Field(description="Data source")
    count: int = Field(description="Number of languages")


class AllLanguagesResponse(BaseModel):
    """Response containing all languages grouped by provider."""

    providers: dict[str, ProviderLanguagesResponse] = Field(
        description="Languages grouped by provider"
    )
    total_languages: int = Field(description="Total unique languages across all providers")
    total_providers: int = Field(description="Number of providers")


class LanguageListResponse(BaseModel):
    """Response containing all known languages."""

    languages: list[LanguageResponse] = Field(description="All known languages")
    count: int = Field(description="Number of languages")


# =============================================================================
# Helper Functions
# =============================================================================


def _convert_language(lang: Language) -> LanguageResponse:
    """Convert internal Language model to response model."""
    return LanguageResponse(
        code=lang.code,
        name=lang.name,
        native_name=lang.native_name,
        region=lang.region,
    )


def _convert_provider(provider: ProviderLanguages) -> ProviderLanguagesResponse:
    """Convert internal ProviderLanguages model to response model."""
    return ProviderLanguagesResponse(
        provider=provider.provider,
        languages=[_convert_language(language) for language in provider.languages],
        source=provider.source,
        count=len(provider.languages),
    )


# =============================================================================
# General Endpoints
# =============================================================================


@router.get("", response_model=LanguageListResponse)
async def get_all_supported_languages():
    """
    Get all known languages with their codes and names.

    Returns a reference list of ISO 639-1 language codes with human-readable names.
    Useful for building language selector UIs.
    """
    languages = get_all_languages()
    return LanguageListResponse(
        languages=[_convert_language(language) for language in languages],
        count=len(languages),
    )


# =============================================================================
# STT Endpoints
# =============================================================================


@router.get("/stt", response_model=AllLanguagesResponse)
async def get_all_stt_languages():
    """
    Get all STT languages grouped by provider.

    Returns languages from multiple providers:
    - **deepgram**: Extracted from LiveKit SDK (35 languages)
    - **cartesia**: Extracted from LiveKit SDK (43 languages)
    - **google**: Extracted from LiveKit SDK (168 languages)
    - **openai**: From YAML config (Whisper - 35+ languages)
    - **assemblyai**: From YAML config
    - **elevenlabs**: From YAML config
    - **groq**: From YAML config
    """
    providers_data = get_stt_languages()

    providers = {}
    all_codes = set()

    for provider_name, provider in providers_data.items():
        converted = _convert_provider(provider)
        providers[provider_name] = converted
        for lang in provider.languages:
            all_codes.add(lang.code)

    return AllLanguagesResponse(
        providers=providers,
        total_languages=len(all_codes),
        total_providers=len(providers),
    )


@router.get("/stt/{provider}", response_model=ProviderLanguagesResponse)
async def get_stt_languages_for_provider(provider: str):
    """
    Get STT languages for a specific provider.

    Valid providers: deepgram, cartesia, google, openai, assemblyai, elevenlabs, groq
    """
    provider_data = get_stt_languages_by_provider(provider)

    if not provider_data:
        available = list(get_stt_languages().keys())
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' not found. Available: {available}"
        )

    return _convert_provider(provider_data)


# =============================================================================
# TTS Endpoints
# =============================================================================


@router.get("/tts", response_model=AllLanguagesResponse)
async def get_all_tts_languages():
    """
    Get all TTS languages grouped by provider.

    Returns languages from multiple providers:
    - **cartesia**: Extracted from LiveKit SDK (7 languages)
    - **openai**: From YAML config
    - **elevenlabs**: From YAML config (multilingual - 29 languages)
    - **deepgram**: From YAML config (English only)
    - **google**: From YAML config
    - **rime**: From YAML config (English only)
    """
    providers_data = get_tts_languages()

    providers = {}
    all_codes = set()

    for provider_name, provider in providers_data.items():
        converted = _convert_provider(provider)
        providers[provider_name] = converted
        for lang in provider.languages:
            all_codes.add(lang.code)

    return AllLanguagesResponse(
        providers=providers,
        total_languages=len(all_codes),
        total_providers=len(providers),
    )


@router.get("/tts/{provider}", response_model=ProviderLanguagesResponse)
async def get_tts_languages_for_provider(provider: str):
    """
    Get TTS languages for a specific provider.

    Valid providers: cartesia, openai, elevenlabs, deepgram, google, rime
    """
    provider_data = get_tts_languages_by_provider(provider)

    if not provider_data:
        available = list(get_tts_languages().keys())
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' not found. Available: {available}"
        )

    return _convert_provider(provider_data)


# =============================================================================
# Realtime Endpoints
# =============================================================================


@router.get("/realtime", response_model=AllLanguagesResponse)
async def get_all_realtime_languages():
    """
    Get all Realtime languages grouped by provider.

    Returns languages from realtime providers:
    - **openai**: OpenAI Realtime API
    - **gemini**: Google Gemini Live
    """
    providers_data = get_realtime_languages()

    providers = {}
    all_codes = set()

    for provider_name, provider in providers_data.items():
        converted = _convert_provider(provider)
        providers[provider_name] = converted
        for lang in provider.languages:
            all_codes.add(lang.code)

    return AllLanguagesResponse(
        providers=providers,
        total_languages=len(all_codes),
        total_providers=len(providers),
    )


@router.get("/realtime/{provider}", response_model=ProviderLanguagesResponse)
async def get_realtime_languages_for_provider(provider: str):
    """
    Get Realtime languages for a specific provider.

    Valid providers: openai, gemini
    """
    provider_data = get_realtime_languages_by_provider(provider)

    if not provider_data:
        available = list(get_realtime_languages().keys())
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' not found. Available: {available}"
        )

    return _convert_provider(provider_data)
