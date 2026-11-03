"""Voices API endpoints.

Provides endpoints for retrieving available TTS and Realtime voices
grouped by provider. Uses a hybrid approach:
- SDK-extracted voices for OpenAI, Google Gemini, Rime
- YAML-defined voices for Cartesia, Deepgram, ElevenLabs, Google Cloud TTS
- ElevenLabs from the account itself when the API has its key
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from src.api.deps import require_auth
from src.api.ratelimit import (
    VOICE_LIBRARY_ADD_LIMIT,
    VOICE_LIBRARY_LIMIT,
    VOICE_PREVIEW_LIMIT,
    limiter,
)
from src.core.security import AuthUser, is_admin_user
from src.services import elevenlabs_voices, voice_preview
from src.services.elevenlabs_voices import LibraryCapacity, LibraryPage
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
# The ElevenLabs library
# =============================================================================


class LibraryAccessResponse(BaseModel):
    """Whether this caller may search the library, and the room the plan has."""

    allowed: bool
    capacity: LibraryCapacity | None = None


class AddLibraryVoiceRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)


class AddLibraryVoiceResponse(BaseModel):
    id: str = Field(description="The voice's id in the account")


#: The two ids go into ElevenLabs' path, so nothing there can change the route.
LibraryId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$", max_length=128)]


def _library_admin(user: Annotated[AuthUser, Depends(require_auth)]) -> AuthUser:
    """The account is the whole deployment's: only an administrator touches it."""
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only an administrator can do this")
    return user


@router.get("/library/access", response_model=LibraryAccessResponse)
@limiter.limit(VOICE_LIBRARY_LIMIT)
async def voice_library_access(
    request: Request,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> LibraryAccessResponse:
    """Whether to offer the library at all: a plain no for everybody else."""
    if not is_admin_user(user) or not elevenlabs_voices.has_key():
        return LibraryAccessResponse(allowed=False)
    return LibraryAccessResponse(allowed=True, capacity=await elevenlabs_voices.library_capacity())


@router.get("/library", response_model=LibraryPage)
@limiter.limit(VOICE_LIBRARY_LIMIT)
async def search_voice_library(
    request: Request,
    _: Annotated[AuthUser, Depends(_library_admin)],
    search: Annotated[str | None, Query(max_length=120)] = None,
    language: Annotated[str | None, Query(pattern=r"^[a-z]{2,3}$")] = None,
    gender: Literal["male", "female", "neutral"] | None = None,
    page: Annotated[int, Query(ge=0, le=1000)] = 0,
) -> LibraryPage:
    """Search ElevenLabs' public library, the most used voices first."""
    return await elevenlabs_voices.search_library(
        search=search, language=language, gender=gender, page=page
    )


@router.post(
    "/library/{owner_id}/{voice_id}",
    response_model=AddLibraryVoiceResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(VOICE_LIBRARY_ADD_LIMIT)
async def add_voice_from_library(
    request: Request,
    _: Annotated[AuthUser, Depends(_library_admin)],
    owner_id: LibraryId,
    voice_id: LibraryId,
    body: AddLibraryVoiceRequest,
) -> AddLibraryVoiceResponse:
    """Save a library voice into the deployment's ElevenLabs account."""
    added = await elevenlabs_voices.add_library_voice(owner_id, voice_id, body.name)
    return AddLibraryVoiceResponse(id=added)


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
