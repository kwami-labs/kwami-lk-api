"""Language configuration service.

This module provides language data for STT, TTS, and Realtime models.
It uses a hybrid approach:
- Extracts languages from LiveKit SDK where available
- Falls back to YAML configuration for providers not in SDK

Last updated: 2026-02-04
"""

import logging
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel

logger = logging.getLogger("kwami-api.languages")


# =============================================================================
# Language Models
# =============================================================================


class Language(BaseModel):
    """Language definition with metadata."""

    code: str
    name: str
    native_name: str | None = None
    region: str | None = None


class ProviderLanguages(BaseModel):
    """Languages supported by a provider."""

    provider: str
    languages: list[Language]
    source: Literal["sdk", "yaml"] = "yaml"


# =============================================================================
# ISO 639-1 Language Code to Name Mapping
# =============================================================================

LANGUAGE_NAMES: dict[str, dict[str, str]] = {
    # Major languages
    "en": {"name": "English", "native": "English"},
    "en-US": {"name": "English (US)", "native": "English", "region": "US"},
    "en-GB": {"name": "English (UK)", "native": "English", "region": "GB"},
    "en-AU": {"name": "English (Australia)", "native": "English", "region": "AU"},
    "en-NZ": {"name": "English (New Zealand)", "native": "English", "region": "NZ"},
    "en-IN": {"name": "English (India)", "native": "English", "region": "IN"},
    # Spanish
    "es": {"name": "Spanish", "native": "Español"},
    "es-419": {"name": "Spanish (Latin America)", "native": "Español", "region": "LATAM"},
    "es-LATAM": {"name": "Spanish (Latin America)", "native": "Español", "region": "LATAM"},
    "es-ES": {"name": "Spanish (Spain)", "native": "Español", "region": "ES"},
    "es-MX": {"name": "Spanish (Mexico)", "native": "Español", "region": "MX"},
    # French
    "fr": {"name": "French", "native": "Français"},
    "fr-CA": {"name": "French (Canada)", "native": "Français", "region": "CA"},
    "fr-FR": {"name": "French (France)", "native": "Français", "region": "FR"},
    # German
    "de": {"name": "German", "native": "Deutsch"},
    "de-DE": {"name": "German (Germany)", "native": "Deutsch", "region": "DE"},
    "de-AT": {"name": "German (Austria)", "native": "Deutsch", "region": "AT"},
    "de-CH": {"name": "German (Switzerland)", "native": "Deutsch", "region": "CH"},
    # Portuguese
    "pt": {"name": "Portuguese", "native": "Português"},
    "pt-BR": {"name": "Portuguese (Brazil)", "native": "Português", "region": "BR"},
    "pt-PT": {"name": "Portuguese (Portugal)", "native": "Português", "region": "PT"},
    # Chinese
    "zh": {"name": "Chinese", "native": "中文"},
    "zh-CN": {"name": "Chinese (Simplified)", "native": "简体中文", "region": "CN"},
    "zh-TW": {"name": "Chinese (Traditional)", "native": "繁體中文", "region": "TW"},
    "zh-HK": {"name": "Chinese (Hong Kong)", "native": "粵語", "region": "HK"},
    # Japanese
    "ja": {"name": "Japanese", "native": "日本語"},
    "ja-JP": {"name": "Japanese (Japan)", "native": "日本語", "region": "JP"},
    # Korean
    "ko": {"name": "Korean", "native": "한국어"},
    "ko-KR": {"name": "Korean (Korea)", "native": "한국어", "region": "KR"},
    # Other major languages
    "it": {"name": "Italian", "native": "Italiano"},
    "nl": {"name": "Dutch", "native": "Nederlands"},
    "pl": {"name": "Polish", "native": "Polski"},
    "ru": {"name": "Russian", "native": "Русский"},
    "ar": {"name": "Arabic", "native": "العربية"},
    "hi": {"name": "Hindi", "native": "हिन्दी"},
    "hi-Latn": {"name": "Hindi (Romanized)", "native": "Hindi", "region": "Latn"},
    "tr": {"name": "Turkish", "native": "Türkçe"},
    "vi": {"name": "Vietnamese", "native": "Tiếng Việt"},
    "th": {"name": "Thai", "native": "ไทย"},
    "id": {"name": "Indonesian", "native": "Bahasa Indonesia"},
    "ms": {"name": "Malay", "native": "Bahasa Melayu"},
    "tl": {"name": "Tagalog", "native": "Tagalog"},
    "fil": {"name": "Filipino", "native": "Filipino"},
    # European languages
    "sv": {"name": "Swedish", "native": "Svenska"},
    "da": {"name": "Danish", "native": "Dansk"},
    "no": {"name": "Norwegian", "native": "Norsk"},
    "fi": {"name": "Finnish", "native": "Suomi"},
    "cs": {"name": "Czech", "native": "Čeština"},
    "sk": {"name": "Slovak", "native": "Slovenčina"},
    "hu": {"name": "Hungarian", "native": "Magyar"},
    "ro": {"name": "Romanian", "native": "Română"},
    "bg": {"name": "Bulgarian", "native": "Български"},
    "hr": {"name": "Croatian", "native": "Hrvatski"},
    "el": {"name": "Greek", "native": "Ελληνικά"},
    "uk": {"name": "Ukrainian", "native": "Українська"},
    "he": {"name": "Hebrew", "native": "עברית"},
    "ka": {"name": "Georgian", "native": "ქართული"},
    # Indian languages
    "ta": {"name": "Tamil", "native": "தமிழ்"},
    "te": {"name": "Telugu", "native": "తెలుగు"},
    "bn": {"name": "Bengali", "native": "বাংলা"},
    "gu": {"name": "Gujarati", "native": "ગુજરાતી"},
    "kn": {"name": "Kannada", "native": "ಕನ್ನಡ"},
    "ml": {"name": "Malayalam", "native": "മലയാളം"},
    "mr": {"name": "Marathi", "native": "मराठी"},
    "or": {"name": "Odia", "native": "ଓଡ଼ିଆ"},
    "pa": {"name": "Punjabi", "native": "ਪੰਜਾਬੀ"},
    # African languages
    "sw": {"name": "Swahili", "native": "Kiswahili"},
    "af": {"name": "Afrikaans", "native": "Afrikaans"},
    "taq": {"name": "Tamasheq", "native": "Tamasheq"},
    # Special
    "multi": {"name": "Multi-language", "native": "Multi-language"},
}


def get_language_info(code: str) -> Language:
    """Get language info from code, with fallback for unknown codes."""
    # Handle non-string codes (e.g., YAML parsing booleans)
    if not isinstance(code, str):
        code = str(code)
    info = LANGUAGE_NAMES.get(code, {})
    return Language(
        code=code,
        name=info.get("name", code.upper()),
        native_name=info.get("native"),
        region=info.get("region"),
    )


# =============================================================================
# SDK Language Extraction
# =============================================================================


def _extract_deepgram_stt_languages() -> list[Language]:
    """Extract Deepgram STT languages from SDK."""
    try:
        from livekit.plugins.deepgram.models import DeepgramLanguages

        codes = get_args(DeepgramLanguages)
        return [get_language_info(code) for code in codes]
    except ImportError as e:
        logger.warning(f"Could not import Deepgram languages: {e}")
        return []


def _extract_cartesia_stt_languages() -> list[Language]:
    """Extract Cartesia STT languages from SDK."""
    try:
        from livekit.plugins.cartesia.models import STTLanguages

        codes = get_args(STTLanguages)
        return [get_language_info(code) for code in codes]
    except ImportError as e:
        logger.warning(f"Could not import Cartesia STT languages: {e}")
        return []


def _extract_cartesia_tts_languages() -> list[Language]:
    """Extract Cartesia TTS languages from SDK."""
    try:
        from livekit.plugins.cartesia.models import TTSLanguages

        codes = get_args(TTSLanguages)
        return [get_language_info(code) for code in codes]
    except ImportError as e:
        logger.warning(f"Could not import Cartesia TTS languages: {e}")
        return []


def _extract_google_stt_languages() -> list[Language]:
    """Extract Google STT languages from SDK."""
    try:
        from livekit.plugins.google.models import SpeechLanguages

        codes = get_args(SpeechLanguages)
        return [get_language_info(code) for code in codes]
    except ImportError as e:
        logger.warning(f"Could not import Google STT languages: {e}")
        return []


# =============================================================================
# YAML Language Loading
# =============================================================================

# Force reload on module import (for dev server hot reload)
_LANGUAGES_YAML: dict | None = None


def _load_languages_yaml() -> dict:
    """Load languages from YAML configuration."""
    global _LANGUAGES_YAML
    if _LANGUAGES_YAML is not None:
        return _LANGUAGES_YAML

    yaml_path = Path(__file__).parents[2] / "config" / "livekit_languages.yaml"
    try:
        with open(yaml_path) as f:
            _LANGUAGES_YAML = yaml.safe_load(f) or {}
            logger.info(f"Loaded languages from {yaml_path}")
            return _LANGUAGES_YAML
    except FileNotFoundError:
        logger.warning(f"Languages YAML not found at {yaml_path}, using empty config")
        _LANGUAGES_YAML = {}
        return _LANGUAGES_YAML
    except Exception as e:
        logger.error(f"Failed to load languages YAML: {e}")
        _LANGUAGES_YAML = {}
        return _LANGUAGES_YAML


def _get_yaml_languages(provider: str, model_type: str) -> list[Language]:
    """Get languages for a provider from YAML config."""
    data = _load_languages_yaml()
    codes = data.get(model_type, {}).get(provider, [])
    return [get_language_info(code) for code in codes]


# =============================================================================
# Public API
# =============================================================================


def get_stt_languages() -> dict[str, ProviderLanguages]:
    """Get all STT languages grouped by provider."""
    providers = {}

    # SDK-extracted languages
    deepgram_langs = _extract_deepgram_stt_languages()
    if deepgram_langs:
        providers["deepgram"] = ProviderLanguages(
            provider="deepgram",
            languages=deepgram_langs,
            source="sdk",
        )

    cartesia_langs = _extract_cartesia_stt_languages()
    if cartesia_langs:
        providers["cartesia"] = ProviderLanguages(
            provider="cartesia",
            languages=cartesia_langs,
            source="sdk",
        )

    google_langs = _extract_google_stt_languages()
    if google_langs:
        providers["google"] = ProviderLanguages(
            provider="google",
            languages=google_langs,
            source="sdk",
        )

    # YAML languages for providers not in SDK
    yaml_providers = ["openai", "assemblyai", "elevenlabs", "groq"]
    for provider in yaml_providers:
        langs = _get_yaml_languages(provider, "stt")
        if langs:
            providers[provider] = ProviderLanguages(
                provider=provider,
                languages=langs,
                source="yaml",
            )

    return providers


def get_stt_languages_by_provider(provider: str) -> ProviderLanguages | None:
    """Get STT languages for a specific provider."""
    all_langs = get_stt_languages()
    return all_langs.get(provider)


def get_tts_languages() -> dict[str, ProviderLanguages]:
    """Get all TTS languages grouped by provider."""
    providers = {}

    # SDK-extracted languages
    cartesia_langs = _extract_cartesia_tts_languages()
    if cartesia_langs:
        providers["cartesia"] = ProviderLanguages(
            provider="cartesia",
            languages=cartesia_langs,
            source="sdk",
        )

    # YAML languages for providers not in SDK
    yaml_providers = ["openai", "elevenlabs", "deepgram", "google", "rime"]
    for provider in yaml_providers:
        langs = _get_yaml_languages(provider, "tts")
        if langs:
            providers[provider] = ProviderLanguages(
                provider=provider,
                languages=langs,
                source="yaml",
            )

    return providers


def get_tts_languages_by_provider(provider: str) -> ProviderLanguages | None:
    """Get TTS languages for a specific provider."""
    all_langs = get_tts_languages()
    return all_langs.get(provider)


def get_realtime_languages() -> dict[str, ProviderLanguages]:
    """Get all Realtime languages grouped by provider."""
    providers = {}

    # Realtime languages are primarily from YAML
    yaml_providers = ["openai", "gemini"]
    for provider in yaml_providers:
        langs = _get_yaml_languages(provider, "realtime")
        if langs:
            providers[provider] = ProviderLanguages(
                provider=provider,
                languages=langs,
                source="yaml",
            )

    return providers


def get_realtime_languages_by_provider(provider: str) -> ProviderLanguages | None:
    """Get Realtime languages for a specific provider."""
    all_langs = get_realtime_languages()
    return all_langs.get(provider)


def get_all_languages() -> list[Language]:
    """Get all known languages (for reference/lookup)."""
    return [get_language_info(code) for code in LANGUAGE_NAMES.keys()]


def reload_languages_yaml():
    """Reload languages from YAML (for development/testing)."""
    global _LANGUAGES_YAML
    _LANGUAGES_YAML = None
    _load_languages_yaml()
