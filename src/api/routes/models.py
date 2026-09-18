"""Models information endpoints.

Provides endpoints for:
- LiveKit Inference models (from YAML config)
- Plugin models (dynamically extracted from SDK)
- Model pricing information
- LLM metrics structure
"""

import logging
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.services.models import (
    get_default_metadata,
    get_model_metadata,
)

logger = logging.getLogger("kwami-api.models")
router = APIRouter()


# =============================================================================
# Load LiveKit Inference Models from YAML
# =============================================================================


def _load_yaml_config(filename: str) -> dict:
    """Load a YAML config file from the project config folder.

    Returns the full parsed dict including 'models' and 'last_updated' fields.
    """
    yaml_path = Path(__file__).parents[3] / "config" / filename
    try:
        with open(yaml_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load {filename} from {yaml_path}: {e}")
        return {}


# Cached YAML data (full dict with models + last_updated)
_INFERENCE_LLM_DATA: dict | None = None
_INFERENCE_STT_DATA: dict | None = None
_INFERENCE_TTS_DATA: dict | None = None


def _get_inference_data(model_type: str) -> dict:
    """Get cached inference YAML data for a model type."""
    global _INFERENCE_LLM_DATA, _INFERENCE_STT_DATA, _INFERENCE_TTS_DATA

    filenames = {
        "llm": "livekit_inference_llm.yaml",
        "stt": "livekit_inference_stt.yaml",
        "tts": "livekit_inference_tts.yaml",
    }
    cache_ref = {"llm": _INFERENCE_LLM_DATA, "stt": _INFERENCE_STT_DATA, "tts": _INFERENCE_TTS_DATA}
    if cache_ref[model_type] is None:
        data = _load_yaml_config(filenames[model_type])
        if model_type == "llm":
            _INFERENCE_LLM_DATA = data
        elif model_type == "stt":
            _INFERENCE_STT_DATA = data
        elif model_type == "tts":
            _INFERENCE_TTS_DATA = data
        return data
    return cache_ref[model_type]


def get_inference_llm_models() -> list[dict]:
    """Get cached inference LLM models."""
    return _get_inference_data("llm").get("models", [])


def get_inference_stt_models() -> list[dict]:
    """Get cached inference STT models."""
    return _get_inference_data("stt").get("models", [])


def get_inference_tts_models() -> list[dict]:
    """Get cached inference TTS models."""
    return _get_inference_data("tts").get("models", [])


def get_last_updated(model_type: str) -> str:
    """Get the last_updated date from the YAML file for a model type."""
    return _get_inference_data(model_type).get("last_updated", "unknown")


# =============================================================================
# Helpers for extracting model definitions from LiveKit SDK Plugins
# =============================================================================


def _extract_literal_values(type_hint: Any) -> list[str]:
    """Extract string values from a Literal or Union type hint."""
    if type_hint is None:
        return []

    origin = get_origin(type_hint)

    if origin is Literal:
        return [str(arg) for arg in get_args(type_hint)]

    if origin is Union:
        values = []
        for arg in get_args(type_hint):
            values.extend(_extract_literal_values(arg))
        return values

    if hasattr(type_hint, "__args__"):
        return [str(arg) for arg in type_hint.__args__ if isinstance(arg, str)]

    return []


def _safe_import_and_extract(module_path: str, type_name: str) -> list[str]:
    """Safely import a module and extract Literal values from a type."""
    try:
        import importlib

        module = importlib.import_module(module_path)
        type_hint = getattr(module, type_name, None)
        if type_hint:
            return _extract_literal_values(type_hint)
    except (ImportError, AttributeError) as e:
        logger.debug(f"Could not import {module_path}.{type_name}: {e}")
    return []


# =============================================================================
# Plugin extraction functions
# =============================================================================


def _get_llm_plugin_models() -> dict[str, list[str]]:
    """Extract LLM models from LiveKit plugins."""
    plugins = {}

    plugin_extractions = [
        ("openai", "livekit.plugins.openai.models", "ChatModels"),
        ("anthropic", "livekit.plugins.anthropic.models", "ChatModels"),
        ("google", "livekit.plugins.google.models", "ChatModels"),
        ("groq", "livekit.plugins.groq.models", "ChatModels"),
        ("mistralai", "livekit.plugins.mistralai.models", "ChatModels"),
        ("cerebras", "livekit.plugins.openai.models", "CerebrasChatModels"),
        ("perplexity", "livekit.plugins.openai.models", "PerplexityChatModels"),
        ("xai", "livekit.plugins.openai.models", "XAIChatModels"),
        ("deepseek", "livekit.plugins.openai.models", "DeepSeekChatModels"),
        ("together", "livekit.plugins.openai.models", "TogetherChatModels"),
        ("telnyx", "livekit.plugins.openai.models", "TelnyxChatModels"),
        ("nebius", "livekit.plugins.openai.models", "NebiusChatModels"),
        ("octoai", "livekit.plugins.openai.models", "OctoChatModels"),
    ]

    for provider, module, type_name in plugin_extractions:
        models = _safe_import_and_extract(module, type_name)
        if models:
            plugins[provider] = models

    return plugins


def _get_stt_models() -> dict[str, Any]:
    """Extract all STT models from LiveKit SDK and plugins."""
    inference = []
    plugins = {}
    source = "sdk"

    try:
        from livekit.agents.inference.stt import STTModels

        inference = _extract_literal_values(STTModels)
    except ImportError:
        inference = []
        source = "fallback"

    plugin_extractions = [
        ("deepgram", "livekit.plugins.deepgram.models", "DeepgramModels"),
        ("google", "livekit.plugins.google.models", "SpeechModels"),
        ("groq", "livekit.plugins.groq.models", "STTModels"),
        ("elevenlabs", "livekit.plugins.elevenlabs.models", "STTModels"),
        ("cartesia", "livekit.plugins.cartesia.models", "STTModels"),
        ("mistralai", "livekit.plugins.mistralai.models", "STTModels"),
    ]

    for provider, module, type_name in plugin_extractions:
        models = _safe_import_and_extract(module, type_name)
        if models:
            plugins[provider] = models

    return {"inference": inference, "plugins": plugins, "source": source}


def _get_tts_models() -> dict[str, Any]:
    """Extract all TTS models from LiveKit SDK and plugins."""
    inference = []
    plugins = {}
    source = "sdk"

    try:
        from livekit.agents.inference.tts import TTSModels

        inference = _extract_literal_values(TTSModels)
    except ImportError:
        inference = []
        source = "fallback"

    plugin_extractions = [
        ("openai", "livekit.plugins.openai.models", "TTSModels"),
        ("deepgram", "livekit.plugins.deepgram.models", "TTSModels"),
        ("elevenlabs", "livekit.plugins.elevenlabs.models", "TTSModels"),
        ("cartesia", "livekit.plugins.cartesia.models", "TTSModels"),
        ("google", "livekit.plugins.google.models", "GeminiTTSModels"),
        ("groq", "livekit.plugins.groq.models", "TTSModels"),
        ("rime", "livekit.plugins.rime.models", "TTSModels"),
    ]

    for provider, module, type_name in plugin_extractions:
        models = _safe_import_and_extract(module, type_name)
        if models:
            plugins[provider] = models

    return {"inference": inference, "plugins": plugins, "source": source}


def _get_realtime_models() -> dict[str, Any]:
    """Extract all Realtime models from LiveKit SDK and plugins."""
    inference = []
    plugins = {}

    openai_rt = _safe_import_and_extract("livekit.plugins.openai.models", "RealtimeModels")
    if openai_rt:
        plugins["openai"] = openai_rt

    google_rt = _safe_import_and_extract(
        "livekit.plugins.google.realtime.api_proto", "LiveAPIModels"
    )
    if google_rt:
        plugins["google"] = google_rt

    aws_rt = _safe_import_and_extract(
        "livekit.plugins.aws.experimental.realtime.types", "REALTIME_MODELS"
    )
    if not aws_rt:
        aws_rt = ["amazon.nova-sonic-v1:0", "amazon.nova-2-sonic-v1:0"]
    plugins["aws"] = aws_rt

    return {"inference": inference, "plugins": plugins, "source": "sdk"}


# =============================================================================
# Response Models
# =============================================================================


class ModelTypeEnum(StrEnum):
    llm = "llm"
    stt = "stt"
    tts = "tts"
    realtime = "realtime"


class ProviderPricing(BaseModel):
    """Pricing for a specific provider."""

    input_per_1m: float
    cached_per_1m: float | None = None
    output_per_1m: float


class InferenceModel(BaseModel):
    """LiveKit Inference model with full metadata."""

    model_id: str
    display_name: str
    provider: str
    context_window: int
    max_output: int | None = None
    capabilities: list[str] = []
    speed: Literal["fast", "standard", "slow"] = "standard"
    tier: Literal["flagship", "standard", "budget"] = "standard"
    description: str | None = None
    providers: dict[str, ProviderPricing] = {}


class InferenceModelsResponse(BaseModel):
    """Response for LiveKit Inference models."""

    last_updated: str = Field(description="When the pricing/metadata was last updated")
    models: list[InferenceModel] = Field(description="All available inference models")


class PluginModel(BaseModel):
    """Plugin model with metadata."""

    model_id: str
    display_name: str
    provider: str
    context_window: int
    max_output: int | None = None
    capabilities: list[str] = []
    speed: Literal["fast", "standard", "slow"] = "standard"
    tier: Literal["flagship", "standard", "budget"] = "standard"


class PluginModelsResponse(BaseModel):
    """Response for plugin models."""

    source: Literal["sdk", "fallback"]
    providers: dict[str, list[PluginModel]]


# STT Models
class STTPricing(BaseModel):
    """Pricing for STT model (per minute)."""

    build_ship_per_min: float
    scale_per_min: float


class InferenceSTTModel(BaseModel):
    """LiveKit Inference STT model with full metadata."""

    model_id: str
    display_name: str
    provider: str
    languages: list[str] = []
    features: list[str] = []
    speed: Literal["fast", "standard", "slow"] = "standard"
    tier: Literal["flagship", "standard", "budget"] = "standard"
    description: str | None = None
    pricing: STTPricing


class InferenceSTTResponse(BaseModel):
    """Response for LiveKit Inference STT models."""

    last_updated: str = Field(description="When the pricing/metadata was last updated")
    models: list[InferenceSTTModel] = Field(description="All available STT inference models")


class PluginSTTModel(BaseModel):
    """Plugin STT model with metadata."""

    model_id: str
    display_name: str
    provider: str
    languages: list[str] = []
    features: list[str] = []
    speed: Literal["fast", "standard", "slow"] = "standard"
    tier: Literal["flagship", "standard", "budget"] = "standard"


class PluginSTTResponse(BaseModel):
    """Response for STT plugin models."""

    source: Literal["sdk", "fallback"]
    providers: dict[str, list[PluginSTTModel]]


# TTS Models
class TTSPricing(BaseModel):
    """Pricing for TTS model (per million characters)."""

    build_ship_per_1m_chars: float
    scale_per_1m_chars: float


class InferenceTTSModel(BaseModel):
    """LiveKit Inference TTS model with full metadata."""

    model_id: str
    display_name: str
    provider: str
    languages: list[str] = []
    features: list[str] = []
    speed: Literal["fast", "standard", "slow"] = "standard"
    tier: Literal["flagship", "standard", "budget"] = "standard"
    description: str | None = None
    pricing: TTSPricing


class InferenceTTSResponse(BaseModel):
    """Response for LiveKit Inference TTS models."""

    last_updated: str = Field(description="When the pricing/metadata was last updated")
    models: list[InferenceTTSModel] = Field(description="All available TTS inference models")


class PluginTTSModel(BaseModel):
    """Plugin TTS model with metadata."""

    model_id: str
    display_name: str
    provider: str
    languages: list[str] = []
    features: list[str] = []
    speed: Literal["fast", "standard", "slow"] = "standard"
    tier: Literal["flagship", "standard", "budget"] = "standard"


class PluginTTSResponse(BaseModel):
    """Response for TTS plugin models."""

    source: Literal["sdk", "fallback"]
    providers: dict[str, list[PluginTTSModel]]


class ModelsResponse(BaseModel):
    """Generic response for STT/TTS/Realtime models."""

    source: Literal["sdk", "fallback"]
    inference: list[str]
    plugins: dict[str, list[str]]


class LLMMetricsSchema(BaseModel):
    """Schema describing the LLMMetrics structure from LiveKit."""

    description: str = Field(
        default="LLMMetrics are emitted by the LLM after each generation. "
        "Subscribe to 'metrics_collected' event on session.llm to receive these."
    )
    fields: dict[str, str] = Field(
        default={
            "timestamp": "float - Unix timestamp when metrics were collected",
            "type": "str - Always 'llm' for LLM metrics",
            "label": "str - Label identifying the metric source",
            "request_id": "str - Unique identifier for the request",
            "duration": "float - Total duration of the request in seconds",
            "ttft": "float - Time to first token in seconds",
            "cancelled": "bool - Whether the request was cancelled",
            "completion_tokens": "int - Number of tokens in the completion",
            "prompt_tokens": "int - Number of tokens in the prompt",
            "total_tokens": "int - Total tokens (prompt + completion)",
            "tokens_per_second": "float - Token generation rate",
        }
    )


class CostEstimate(BaseModel):
    """Estimated cost based on token usage."""

    model_id: str
    prompt_tokens: int
    completion_tokens: int
    prompt_cost_usd: float
    completion_cost_usd: float
    total_cost_usd: float
    cached_tokens: int | None = None
    cached_cost_usd: float | None = None


class ModelTypeCapabilities(BaseModel):
    """Capabilities for a model type."""

    description: str
    input: list[str]
    output: list[str]
    features: list[str]
    optional_input: list[str] | None = None


class VisionCapableModels(BaseModel):
    """Models that support vision/image input."""

    description: str = "LLM models that can process images and video frames"
    models: dict[str, list[str]]
    usage_notes: str


class VideoInputModels(BaseModel):
    """Models that support live video input."""

    description: str = "Realtime models with native video stream support"
    models: dict[str, list[str]]
    usage_notes: str


class CapabilitiesResponse(BaseModel):
    """Complete capabilities overview for all model types."""

    llm: ModelTypeCapabilities
    stt: ModelTypeCapabilities
    tts: ModelTypeCapabilities
    realtime: ModelTypeCapabilities
    vision: VisionCapableModels
    video_input: VideoInputModels


# =============================================================================
# Capabilities Data
# =============================================================================


def _get_capabilities() -> CapabilitiesResponse:
    """Build the capabilities response with all model type info."""
    return CapabilitiesResponse(
        llm=ModelTypeCapabilities(
            description="Large Language Models for text generation and reasoning",
            input=["text", "images"],
            output=["text"],
            features=[
                "streaming",
                "function_calling",
                "vision",
                "structured_output",
                "context_caching",
            ],
            optional_input=["images (vision-capable models only)"],
        ),
        stt=ModelTypeCapabilities(
            description="Speech-to-Text models for transcription",
            input=["audio"],
            output=["text"],
            features=[
                "streaming",
                "word_timestamps",
                "language_detection",
                "punctuation",
                "speaker_diarization",
            ],
        ),
        tts=ModelTypeCapabilities(
            description="Text-to-Speech models for voice synthesis",
            input=["text"],
            output=["audio"],
            features=[
                "streaming",
                "multiple_voices",
                "voice_cloning",
                "ssml_support",
                "emotion_control",
            ],
        ),
        realtime=ModelTypeCapabilities(
            description="Realtime multimodal models for bidirectional voice conversations",
            input=["audio", "text"],
            output=["audio", "text"],
            features=[
                "bidirectional_streaming",
                "low_latency",
                "turn_detection",
                "interruption_handling",
                "function_calling",
            ],
            optional_input=["video (some models)"],
        ),
        vision=VisionCapableModels(
            models={
                "openai": [
                    "gpt-4o",
                    "gpt-4o-mini",
                    "gpt-4-turbo",
                    "gpt-4.1",
                    "gpt-4.1-mini",
                    "gpt-5",
                    "gpt-5-mini",
                    "gpt-5.1",
                    "gpt-5.2",
                ],
                "anthropic": [
                    "claude-3-5-sonnet-latest",
                    "claude-3-opus-latest",
                    "claude-3-haiku-20240307",
                ],
                "google": [
                    "gemini-2.0-flash",
                    "gemini-2.5-flash",
                    "gemini-2.5-pro",
                    "gemini-3-flash",
                    "gemini-3-pro",
                ],
            },
            usage_notes="Vision models can process static images added to chat context. Use ImageContent to add images.",
        ),
        video_input=VideoInputModels(
            models={
                "openai": ["gpt-4o-realtime-preview"],
                "google": ["gemini-live-2.5-flash-native-audio", "gemini-2.0-flash-exp"],
            },
            usage_notes="These models can process continuous video streams. Enable with video_input=True.",
        ),
    )


# =============================================================================
# Endpoints - LLM Models
# =============================================================================


@router.get("/llm", response_model=InferenceModelsResponse)
async def get_llm_inference_models():
    """
    Get all LiveKit Inference LLM models with full metadata and pricing.

    Returns models available via LiveKit Inference (hosted, no API keys needed).
    Each model includes:
    - **context_window**: Maximum context size in tokens
    - **max_output**: Maximum output tokens
    - **capabilities**: vision, function_calling, json_mode, streaming
    - **speed**: fast, standard, slow
    - **tier**: flagship, standard, budget
    - **providers**: Available providers (azure, openai, google, baseten) with pricing
    """
    models_data = get_inference_llm_models()

    models = []
    for m in models_data:
        providers = {}
        for prov_name, prov_pricing in m.get("providers", {}).items():
            providers[prov_name] = ProviderPricing(
                input_per_1m=prov_pricing["input_per_1m"],
                cached_per_1m=prov_pricing.get("cached_per_1m"),
                output_per_1m=prov_pricing["output_per_1m"],
            )

        models.append(
            InferenceModel(
                model_id=m["model_id"],
                display_name=m["display_name"],
                provider=m["provider"],
                context_window=m["context_window"],
                max_output=m.get("max_output"),
                capabilities=m.get("capabilities", []),
                speed=m.get("speed", "standard"),
                tier=m.get("tier", "standard"),
                description=m.get("description"),
                providers=providers,
            )
        )

    return InferenceModelsResponse(
        last_updated=get_last_updated("llm"),
        models=models,
    )


@router.get("/llm/plugins", response_model=PluginModelsResponse)
async def get_llm_plugin_models():
    """
    Get all LLM models available via LiveKit plugins.

    These require your own API keys for the respective providers.
    Providers include: OpenAI, Anthropic, Google, Groq, Mistral, and more.
    """
    plugins_raw = _get_llm_plugin_models()

    providers: dict[str, list[PluginModel]] = {}
    for provider, model_ids in plugins_raw.items():
        provider_models = []
        for model_id in model_ids:
            # Get metadata or generate default
            metadata = get_model_metadata(model_id)
            if not metadata:
                metadata = get_default_metadata(model_id, provider)

            provider_models.append(
                PluginModel(
                    model_id=model_id,
                    display_name=metadata.display_name,
                    provider=provider,
                    context_window=metadata.context_window,
                    max_output=metadata.max_output,
                    capabilities=metadata.capabilities,
                    speed=metadata.speed,
                    tier=metadata.tier,
                )
            )
        providers[provider] = provider_models

    return PluginModelsResponse(
        source="sdk",
        providers=providers,
    )


# =============================================================================
# Endpoints - STT/TTS/Realtime Models
# =============================================================================


@router.get("/stt", response_model=InferenceSTTResponse)
async def get_stt_inference_models():
    """
    Get all LiveKit Inference STT models with full metadata and pricing.

    Returns models available via LiveKit Inference (hosted, no API keys needed).
    Each model includes:
    - **languages**: Supported languages (en, multilingual)
    - **features**: streaming, punctuation, word_timestamps, diarization, etc.
    - **speed**: fast, standard, slow
    - **tier**: flagship, standard, budget
    - **pricing**: USD per minute (build/ship and scale tiers)
    """
    models_data = get_inference_stt_models()

    models = []
    for m in models_data:
        pricing_data = m.get("pricing", {})
        models.append(
            InferenceSTTModel(
                model_id=m["model_id"],
                display_name=m["display_name"],
                provider=m["provider"],
                languages=m.get("languages", []),
                features=m.get("features", []),
                speed=m.get("speed", "standard"),
                tier=m.get("tier", "standard"),
                description=m.get("description"),
                pricing=STTPricing(
                    build_ship_per_min=pricing_data.get("build_ship_per_min", 0),
                    scale_per_min=pricing_data.get("scale_per_min", 0),
                ),
            )
        )

    return InferenceSTTResponse(
        last_updated=get_last_updated("stt"),
        models=models,
    )


@router.get("/stt/plugins", response_model=PluginSTTResponse)
async def get_stt_plugin_models():
    """
    Get all STT models available via LiveKit plugins.

    These require your own API keys for the respective providers.
    """
    # Provider-aware language and feature defaults
    stt_provider_info = {
        "deepgram": {
            "languages": ["en", "multilingual"],
            "features": ["streaming", "punctuation", "smart_format"],
        },
        "google": {"languages": ["en", "multilingual"], "features": ["streaming", "punctuation"]},
        "groq": {"languages": ["en", "multilingual"], "features": ["streaming"]},
        "elevenlabs": {"languages": ["en", "multilingual"], "features": ["streaming"]},
        "cartesia": {"languages": ["en", "multilingual"], "features": ["streaming"]},
        "mistralai": {"languages": ["en", "multilingual"], "features": ["streaming"]},
    }

    result = _get_stt_models()

    providers: dict[str, list[PluginSTTModel]] = {}
    for provider, model_ids in result["plugins"].items():
        info = stt_provider_info.get(provider, {"languages": ["en"], "features": ["streaming"]})
        provider_models = []
        for model_id in model_ids:
            # Generate display name from model_id
            display_name = model_id.replace("-", " ").replace("_", " ").title()

            provider_models.append(
                PluginSTTModel(
                    model_id=model_id,
                    display_name=display_name,
                    provider=provider,
                    languages=info["languages"],
                    features=info["features"],
                    speed="fast",
                    tier="standard",
                )
            )
        if provider_models:
            providers[provider] = provider_models

    return PluginSTTResponse(
        source=result["source"],
        providers=providers,
    )


@router.get("/tts", response_model=InferenceTTSResponse)
async def get_tts_inference_models():
    """
    Get all LiveKit Inference TTS models with full metadata and pricing.

    Returns models available via LiveKit Inference (hosted, no API keys needed).
    Each model includes:
    - **languages**: Supported languages (en, multilingual)
    - **features**: streaming, low_latency, voice_cloning, emotion_control, etc.
    - **speed**: fast, standard, slow
    - **tier**: flagship, standard, budget
    - **pricing**: USD per million characters (build/ship and scale tiers)
    """
    models_data = get_inference_tts_models()

    models = []
    for m in models_data:
        pricing_data = m.get("pricing", {})
        models.append(
            InferenceTTSModel(
                model_id=m["model_id"],
                display_name=m["display_name"],
                provider=m["provider"],
                languages=m.get("languages", []),
                features=m.get("features", []),
                speed=m.get("speed", "standard"),
                tier=m.get("tier", "standard"),
                description=m.get("description"),
                pricing=TTSPricing(
                    build_ship_per_1m_chars=pricing_data.get("build_ship_per_1m_chars", 0),
                    scale_per_1m_chars=pricing_data.get("scale_per_1m_chars", 0),
                ),
            )
        )

    return InferenceTTSResponse(
        last_updated=get_last_updated("tts"),
        models=models,
    )


@router.get("/tts/plugins", response_model=PluginTTSResponse)
async def get_tts_plugin_models():
    """
    Get all TTS models available via LiveKit plugins.

    These require your own API keys for the respective providers.
    """
    # Provider-aware language and feature defaults
    tts_provider_info = {
        "openai": {"languages": ["en"], "features": ["streaming"]},
        "elevenlabs": {
            "languages": ["en", "multilingual"],
            "features": ["streaming", "voice_cloning"],
        },
        "cartesia": {
            "languages": ["en", "multilingual"],
            "features": ["streaming", "low_latency", "voice_cloning"],
        },
        "deepgram": {"languages": ["en"], "features": ["streaming", "low_latency"]},
        "google": {"languages": ["en", "multilingual"], "features": ["streaming"]},
        "groq": {"languages": ["en", "multilingual"], "features": ["streaming"]},
        "rime": {
            "languages": ["en", "es", "fr", "de"],
            "features": ["streaming", "low_latency", "expressive"],
        },
    }

    result = _get_tts_models()

    providers: dict[str, list[PluginTTSModel]] = {}
    for provider, model_ids in result["plugins"].items():
        info = tts_provider_info.get(provider, {"languages": ["en"], "features": ["streaming"]})
        provider_models = []
        for model_id in model_ids:
            # Generate display name from model_id
            display_name = model_id.replace("-", " ").replace("_", " ").title()

            provider_models.append(
                PluginTTSModel(
                    model_id=model_id,
                    display_name=display_name,
                    provider=provider,
                    languages=info["languages"],
                    features=info["features"],
                    speed="fast",
                    tier="standard",
                )
            )
        if provider_models:
            providers[provider] = provider_models

    return PluginTTSResponse(
        source=result["source"],
        providers=providers,
    )


@router.get("/realtime", response_model=ModelsResponse)
async def get_realtime_models():
    """
    Get all available Realtime (multimodal voice) models.

    Realtime models support bidirectional audio streaming with optional video.
    """
    result = _get_realtime_models()
    return ModelsResponse(
        source=result["source"],
        inference=result["inference"],
        plugins=result["plugins"],
    )


# =============================================================================
# Endpoints - Capabilities
# =============================================================================


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_model_capabilities():
    """
    Get capabilities overview for all model types.

    Returns detailed information about input/output types, features,
    vision-capable models, and video input models.
    """
    return _get_capabilities()


# =============================================================================
# Endpoints - Metrics
# =============================================================================


@router.get("/metrics/llm", response_model=LLMMetricsSchema)
async def get_llm_metrics_schema():
    """
    Get the schema and usage information for LLMMetrics.
    """
    return LLMMetricsSchema()


@router.post("/estimate-cost", response_model=CostEstimate)
async def estimate_cost(
    model_id: str = Query(..., description="Model ID (e.g., 'openai/gpt-4o-mini')"),
    prompt_tokens: int = Query(..., ge=0, description="Number of prompt tokens"),
    completion_tokens: int = Query(..., ge=0, description="Number of completion tokens"),
    cached_tokens: int = Query(0, ge=0, description="Number of cached prompt tokens"),
    provider: str = Query(
        "openai", description="Provider to use for pricing (azure, openai, google, baseten)"
    ),
):
    """
    Estimate the cost for a given token usage.

    Specify the provider to get accurate pricing (azure vs openai may differ slightly).
    """
    models = get_inference_llm_models()
    model_data = next((m for m in models if m["model_id"] == model_id), None)

    if not model_data:
        raise HTTPException(
            status_code=404,
            detail=f"Model not found: {model_id}. Check /models/llm for valid model IDs.",
        )

    providers = model_data.get("providers", {})
    if provider not in providers:
        available = list(providers.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{provider}' not available for {model_id}. Available: {available}",
        )

    pricing = providers[provider]
    input_per_1m = pricing["input_per_1m"]
    output_per_1m = pricing["output_per_1m"]
    cached_per_1m = pricing.get("cached_per_1m")

    non_cached_prompt = prompt_tokens - cached_tokens
    prompt_cost = (non_cached_prompt / 1_000_000) * input_per_1m
    completion_cost = (completion_tokens / 1_000_000) * output_per_1m

    cached_cost = None
    if cached_tokens > 0 and cached_per_1m:
        cached_cost = (cached_tokens / 1_000_000) * cached_per_1m
        prompt_cost += cached_cost

    return CostEstimate(
        model_id=model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_cost_usd=round(prompt_cost, 6),
        completion_cost_usd=round(completion_cost, 6),
        total_cost_usd=round(prompt_cost + completion_cost, 6),
        cached_tokens=cached_tokens if cached_tokens > 0 else None,
        cached_cost_usd=round(cached_cost, 6) if cached_cost else None,
    )
