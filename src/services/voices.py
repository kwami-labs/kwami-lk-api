"""Voice configuration service.

This module provides voice data for TTS and Realtime models.
It uses a hybrid approach:
- Extracts voices from LiveKit SDK where available (OpenAI, Google Gemini, Rime)
- Falls back to YAML configuration for providers without SDK types (Cartesia, Deepgram, ElevenLabs, Google Cloud TTS)

Last updated: 2026-02-04
"""

import logging
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel

logger = logging.getLogger("kwami-api.voices")


# =============================================================================
# Voice Models
# =============================================================================


class Voice(BaseModel):
    """Voice definition with metadata."""

    id: str
    name: str
    category: str | None = None
    gender: Literal["male", "female", "neutral"] | None = None
    language: str | None = None
    description: str | None = None


class VoiceProvider(BaseModel):
    """Voice provider with list of voices."""

    provider: str
    voices: list[Voice]
    source: Literal["sdk", "yaml"] = "yaml"


# =============================================================================
# SDK Voice Extraction
# =============================================================================


def _extract_openai_tts_voices() -> list[Voice]:
    """Extract OpenAI TTS voices from SDK."""
    try:
        from livekit.plugins.openai.models import TTSVoices

        voice_ids = get_args(TTSVoices)

        # Voice metadata (not in SDK, manually defined)
        voice_meta = {
            "alloy": {"name": "Alloy", "category": "Neutral", "gender": "neutral"},
            "ash": {"name": "Ash", "category": "Male", "gender": "male"},
            "ballad": {"name": "Ballad", "category": "Male", "gender": "male"},
            "coral": {"name": "Coral", "category": "Female", "gender": "female"},
            "echo": {"name": "Echo", "category": "Male", "gender": "male"},
            "fable": {"name": "Fable", "category": "Neutral", "gender": "neutral"},
            "nova": {"name": "Nova", "category": "Female", "gender": "female"},
            "onyx": {"name": "Onyx", "category": "Male", "gender": "male"},
            "sage": {"name": "Sage", "category": "Female", "gender": "female"},
            "shimmer": {"name": "Shimmer", "category": "Female", "gender": "female"},
        }

        voices = []
        for vid in voice_ids:
            meta = voice_meta.get(vid, {"name": vid.title(), "category": "Unknown"})
            voices.append(
                Voice(
                    id=vid,
                    name=meta["name"],
                    category=meta.get("category"),
                    gender=meta.get("gender"),
                    language="en",
                )
            )
        return voices
    except ImportError as e:
        logger.warning("Could not import OpenAI TTS voices: %s", e)
        return []


def _extract_openai_realtime_voices() -> list[Voice]:
    """Extract OpenAI Realtime voices (same as TTS voices)."""
    # OpenAI Realtime uses the same voice set as TTS
    return _extract_openai_tts_voices()


def _extract_gemini_realtime_voices() -> list[Voice]:
    """Extract Google Gemini Live voices from SDK."""
    try:
        from livekit.plugins.google.realtime.api_proto import Voice as GeminiVoices

        voice_ids = get_args(GeminiVoices)

        voices = []
        for vid in voice_ids:
            voices.append(
                Voice(
                    id=vid,
                    name=vid,
                    category="Gemini Live",
                    language="multilingual",
                )
            )
        return voices
    except ImportError as e:
        logger.warning("Could not import Gemini Live voices: %s", e)
        return []


def _extract_rime_voices() -> list[Voice]:
    """Extract Rime Arcana voices from SDK."""
    try:
        from livekit.plugins.rime.models import ArcanaVoices

        voice_ids = get_args(ArcanaVoices)

        voices = []
        for vid in voice_ids:
            voices.append(
                Voice(
                    id=vid,
                    name=vid.title(),
                    category="Arcana",
                    language="en",
                )
            )
        return voices
    except ImportError as e:
        logger.warning("Could not import Rime voices: %s", e)
        return []


# =============================================================================
# YAML Voice Loading
# =============================================================================

_VOICES_YAML: dict | None = None


def _load_voices_yaml() -> dict:
    """Load voices from YAML configuration."""
    global _VOICES_YAML
    if _VOICES_YAML is not None:
        return _VOICES_YAML

    yaml_path = Path(__file__).parents[2] / "config" / "livekit_voices.yaml"
    try:
        with open(yaml_path) as f:
            _VOICES_YAML = yaml.safe_load(f) or {}
            logger.info("Loaded voices from %s", yaml_path)
            return _VOICES_YAML
    except FileNotFoundError:
        logger.warning("Voices YAML not found at %s, using empty config", yaml_path)
        _VOICES_YAML = {}
        return _VOICES_YAML
    except Exception:
        logger.exception("Failed to load voices YAML")
        _VOICES_YAML = {}
        return _VOICES_YAML


def _get_yaml_voices(provider: str, voice_type: Literal["tts", "realtime"]) -> list[Voice]:
    """Get voices for a provider from YAML config."""
    data = _load_voices_yaml()
    provider_data = data.get(voice_type, {}).get(provider, [])

    voices = []
    for v in provider_data:
        voices.append(
            Voice(
                id=v["id"],
                name=v["name"],
                category=v.get("category"),
                gender=v.get("gender"),
                language=v.get("language"),
                description=v.get("description"),
            )
        )
    return voices


# =============================================================================
# Public API
# =============================================================================


def get_tts_voices() -> dict[str, VoiceProvider]:
    """Get all TTS voices grouped by provider."""
    providers = {}

    # SDK-extracted voices
    openai_voices = _extract_openai_tts_voices()
    if openai_voices:
        providers["openai"] = VoiceProvider(
            provider="openai",
            voices=openai_voices,
            source="sdk",
        )

    rime_voices = _extract_rime_voices()
    if rime_voices:
        providers["rime"] = VoiceProvider(
            provider="rime",
            voices=rime_voices,
            source="sdk",
        )

    # YAML voices
    yaml_providers = ["cartesia", "elevenlabs", "deepgram", "google"]
    for provider in yaml_providers:
        voices = _get_yaml_voices(provider, "tts")
        if voices:
            providers[provider] = VoiceProvider(
                provider=provider,
                voices=voices,
                source="yaml",
            )

    return providers


def get_tts_voices_by_provider(provider: str) -> VoiceProvider | None:
    """Get TTS voices for a specific provider."""
    all_voices = get_tts_voices()
    return all_voices.get(provider)


def get_realtime_voices() -> dict[str, VoiceProvider]:
    """Get all Realtime voices grouped by provider."""
    providers = {}

    # SDK-extracted voices
    openai_voices = _extract_openai_realtime_voices()
    if openai_voices:
        providers["openai"] = VoiceProvider(
            provider="openai",
            voices=openai_voices,
            source="sdk",
        )

    gemini_voices = _extract_gemini_realtime_voices()
    if gemini_voices:
        providers["gemini"] = VoiceProvider(
            provider="gemini",
            voices=gemini_voices,
            source="sdk",
        )

    # YAML voices (if any additional realtime providers)
    yaml_data = _load_voices_yaml()
    for provider, voices_data in yaml_data.get("realtime", {}).items():
        if provider not in providers:
            voices = [Voice(**v) for v in voices_data]
            providers[provider] = VoiceProvider(
                provider=provider,
                voices=voices,
                source="yaml",
            )

    return providers


def get_realtime_voices_by_provider(provider: str) -> VoiceProvider | None:
    """Get Realtime voices for a specific provider."""
    all_voices = get_realtime_voices()
    return all_voices.get(provider)


def reload_voices_yaml():
    """Reload voices from YAML (for development/testing)."""
    global _VOICES_YAML
    _VOICES_YAML = None
    _load_voices_yaml()
