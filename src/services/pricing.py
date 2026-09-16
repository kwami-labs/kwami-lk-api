"""Canonical provider pricing used by the credits ledger.

This module is the API's source of truth for converting measured provider usage
into raw provider cost. Customer billing policy is applied later on top of these
raw costs so the ledger can report revenue and margin separately.
"""

from typing import Literal

from pydantic import BaseModel

from src.core.config import settings

PRICING_VERSION = settings.billing_pricing_version


class TokenPricing(BaseModel):
    """Pricing for token-based models (LLM)."""

    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float | None = None


class AudioPricing(BaseModel):
    """Pricing for audio-based models (STT/TTS)."""

    per_minute: float | None = None
    per_1m_characters: float | None = None


class RealtimePricing(BaseModel):
    """Pricing for realtime models (audio and optional text units)."""

    audio_input_per_minute: float
    audio_output_per_minute: float
    text_input_per_1m: float | None = None
    text_output_per_1m: float | None = None


class ExternalPricing(BaseModel):
    """Pricing for request-based external services such as search or memory."""

    per_call: float


class ModelPricing(BaseModel):
    """Complete provider pricing info for a billable usage source."""

    model_id: str
    provider: str
    model_type: Literal["llm", "stt", "tts", "realtime", "tool", "memory"]
    display_name: str
    pricing: TokenPricing | AudioPricing | RealtimePricing | ExternalPricing
    notes: str | None = None


# =============================================================================
# LLM Models Pricing
# =============================================================================

LLM_PRICING: dict[str, ModelPricing] = {
    # OpenAI Models (via LiveKit Inference)
    "openai/gpt-4o": ModelPricing(
        model_id="openai/gpt-4o",
        provider="openai",
        model_type="llm",
        display_name="GPT-4o",
        pricing=TokenPricing(
            input_per_1m=2.50,
            output_per_1m=10.00,
            cached_input_per_1m=1.25,
        ),
    ),
    "openai/gpt-4o-mini": ModelPricing(
        model_id="openai/gpt-4o-mini",
        provider="openai",
        model_type="llm",
        display_name="GPT-4o Mini",
        pricing=TokenPricing(
            input_per_1m=0.15,
            output_per_1m=0.60,
            cached_input_per_1m=0.075,
        ),
    ),
    "openai/gpt-4.1": ModelPricing(
        model_id="openai/gpt-4.1",
        provider="openai",
        model_type="llm",
        display_name="GPT-4.1",
        pricing=TokenPricing(
            input_per_1m=2.00,
            output_per_1m=8.00,
            cached_input_per_1m=0.50,
        ),
    ),
    "openai/gpt-4.1-mini": ModelPricing(
        model_id="openai/gpt-4.1-mini",
        provider="openai",
        model_type="llm",
        display_name="GPT-4.1 Mini",
        pricing=TokenPricing(
            input_per_1m=0.40,
            output_per_1m=1.60,
            cached_input_per_1m=0.10,
        ),
    ),
    "openai/gpt-4.1-nano": ModelPricing(
        model_id="openai/gpt-4.1-nano",
        provider="openai",
        model_type="llm",
        display_name="GPT-4.1 Nano",
        pricing=TokenPricing(
            input_per_1m=0.10,
            output_per_1m=0.40,
            cached_input_per_1m=0.025,
        ),
    ),
    # Google Models (via LiveKit Inference)
    "google/gemini-2.0-flash": ModelPricing(
        model_id="google/gemini-2.0-flash",
        provider="google",
        model_type="llm",
        display_name="Gemini 2.0 Flash",
        pricing=TokenPricing(
            input_per_1m=0.10,
            output_per_1m=0.40,
            cached_input_per_1m=0.025,
        ),
    ),
    "google/gemini-2.0-flash-lite": ModelPricing(
        model_id="google/gemini-2.0-flash-lite",
        provider="google",
        model_type="llm",
        display_name="Gemini 2.0 Flash Lite",
        pricing=TokenPricing(
            input_per_1m=0.075,
            output_per_1m=0.30,
            cached_input_per_1m=0.01875,
        ),
    ),
    "google/gemini-2.5-flash": ModelPricing(
        model_id="google/gemini-2.5-flash",
        provider="google",
        model_type="llm",
        display_name="Gemini 2.5 Flash",
        pricing=TokenPricing(
            input_per_1m=0.15,
            output_per_1m=0.60,
            cached_input_per_1m=0.0375,
        ),
    ),
    "google/gemini-2.5-pro": ModelPricing(
        model_id="google/gemini-2.5-pro",
        provider="google",
        model_type="llm",
        display_name="Gemini 2.5 Pro",
        pricing=TokenPricing(
            input_per_1m=1.25,
            output_per_1m=10.00,
            cached_input_per_1m=0.3125,
        ),
    ),
    # DeepSeek Models (via LiveKit Inference)
    "deepseek-ai/deepseek-v3": ModelPricing(
        model_id="deepseek-ai/deepseek-v3",
        provider="deepseek",
        model_type="llm",
        display_name="DeepSeek V3",
        pricing=TokenPricing(
            input_per_1m=0.27,
            output_per_1m=1.10,
            cached_input_per_1m=0.07,
        ),
    ),
    # Kimi Models (via LiveKit Inference)
    "moonshotai/kimi-k2-instruct": ModelPricing(
        model_id="moonshotai/kimi-k2-instruct",
        provider="moonshot",
        model_type="llm",
        display_name="Kimi K2 Instruct",
        pricing=TokenPricing(
            input_per_1m=0.60,
            output_per_1m=2.40,
        ),
    ),
}

# =============================================================================
# STT Models Pricing
# =============================================================================

STT_PRICING: dict[str, ModelPricing] = {
    "deepgram/nova-3-general": ModelPricing(
        model_id="deepgram/nova-3-general",
        provider="deepgram",
        model_type="stt",
        display_name="Deepgram Nova 3",
        pricing=AudioPricing(per_minute=0.0043),
    ),
    "deepgram/nova-2-general": ModelPricing(
        model_id="deepgram/nova-2-general",
        provider="deepgram",
        model_type="stt",
        display_name="Deepgram Nova 2",
        pricing=AudioPricing(per_minute=0.0043),
    ),
    "assemblyai/universal": ModelPricing(
        model_id="assemblyai/universal",
        provider="assemblyai",
        model_type="stt",
        display_name="AssemblyAI Universal",
        pricing=AudioPricing(per_minute=0.0037),
    ),
}

# =============================================================================
# TTS Models Pricing
# =============================================================================

TTS_PRICING: dict[str, ModelPricing] = {
    "cartesia/sonic-3": ModelPricing(
        model_id="cartesia/sonic-3",
        provider="cartesia",
        model_type="tts",
        display_name="Cartesia Sonic 3",
        pricing=AudioPricing(per_1m_characters=15.00),
    ),
    "cartesia/sonic-2": ModelPricing(
        model_id="cartesia/sonic-2",
        provider="cartesia",
        model_type="tts",
        display_name="Cartesia Sonic 2",
        pricing=AudioPricing(per_1m_characters=15.00),
    ),
    "elevenlabs/eleven_turbo_v2_5": ModelPricing(
        model_id="elevenlabs/eleven_turbo_v2_5",
        provider="elevenlabs",
        model_type="tts",
        display_name="ElevenLabs Turbo v2.5",
        pricing=AudioPricing(per_1m_characters=30.00),
    ),
    "openai/tts-1": ModelPricing(
        model_id="openai/tts-1",
        provider="openai",
        model_type="tts",
        display_name="OpenAI TTS-1",
        pricing=AudioPricing(per_1m_characters=15.00),
    ),
    "openai/tts-1-hd": ModelPricing(
        model_id="openai/tts-1-hd",
        provider="openai",
        model_type="tts",
        display_name="OpenAI TTS-1 HD",
        pricing=AudioPricing(per_1m_characters=30.00),
    ),
}

# =============================================================================
# Realtime Models Pricing
# =============================================================================

REALTIME_PRICING: dict[str, ModelPricing] = {
    "openai/gpt-4o-realtime-preview": ModelPricing(
        model_id="openai/gpt-4o-realtime-preview",
        provider="openai",
        model_type="realtime",
        display_name="GPT-4o Realtime",
        pricing=RealtimePricing(
            audio_input_per_minute=0.06,
            audio_output_per_minute=0.24,
            text_input_per_1m=5.00,
            text_output_per_1m=20.00,
        ),
    ),
    "openai/gpt-4o-mini-realtime-preview": ModelPricing(
        model_id="openai/gpt-4o-mini-realtime-preview",
        provider="openai",
        model_type="realtime",
        display_name="GPT-4o Mini Realtime",
        pricing=RealtimePricing(
            audio_input_per_minute=0.01,
            audio_output_per_minute=0.04,
            text_input_per_1m=0.60,
            text_output_per_1m=2.40,
        ),
    ),
}

# =============================================================================
# External Services Pricing
# =============================================================================

EXTERNAL_PRICING: dict[str, ModelPricing] = {
    "tavily/search": ModelPricing(
        model_id="tavily/search",
        provider="tavily",
        model_type="tool",
        display_name="Tavily Search",
        pricing=ExternalPricing(per_call=settings.billing_tavily_search_per_call_usd),
        notes="Configure BILLING_TAVILY_SEARCH_PER_CALL_USD for your Tavily plan.",
    ),
    "tavily/extract": ModelPricing(
        model_id="tavily/extract",
        provider="tavily",
        model_type="tool",
        display_name="Tavily Extract",
        pricing=ExternalPricing(per_call=settings.billing_tavily_extract_per_call_usd),
        notes="Configure BILLING_TAVILY_EXTRACT_PER_CALL_USD for your Tavily plan.",
    ),
    "serpapi/google_shopping": ModelPricing(
        model_id="serpapi/google_shopping",
        provider="serpapi",
        model_type="tool",
        display_name="SerpApi Google Shopping",
        pricing=ExternalPricing(per_call=settings.billing_serpapi_search_per_call_usd),
        notes="Configure BILLING_SERPAPI_SEARCH_PER_CALL_USD for your SerpApi plan.",
    ),
    "microlink/fetch": ModelPricing(
        model_id="microlink/fetch",
        provider="microlink",
        model_type="tool",
        display_name="Microlink Fetch",
        pricing=ExternalPricing(per_call=settings.billing_microlink_fetch_per_call_usd),
        notes="Configure BILLING_MICROLINK_FETCH_PER_CALL_USD if Microlink is billable on your plan.",
    ),
    "zep/add_messages": ModelPricing(
        model_id="zep/add_messages",
        provider="zep",
        model_type="memory",
        display_name="Zep Add Messages",
        pricing=ExternalPricing(per_call=settings.billing_zep_add_messages_per_call_usd),
        notes="Configure BILLING_ZEP_ADD_MESSAGES_PER_CALL_USD for your Zep plan.",
    ),
    "zep/get_context": ModelPricing(
        model_id="zep/get_context",
        provider="zep",
        model_type="memory",
        display_name="Zep Get Context",
        pricing=ExternalPricing(per_call=settings.billing_zep_get_context_per_call_usd),
        notes="Configure BILLING_ZEP_GET_CONTEXT_PER_CALL_USD for your Zep plan.",
    ),
    "zep/thread_search": ModelPricing(
        model_id="zep/thread_search",
        provider="zep",
        model_type="memory",
        display_name="Zep Thread Search",
        pricing=ExternalPricing(per_call=settings.billing_zep_search_per_call_usd),
    ),
    "zep/graph_search": ModelPricing(
        model_id="zep/graph_search",
        provider="zep",
        model_type="memory",
        display_name="Zep Graph Search",
        pricing=ExternalPricing(per_call=settings.billing_zep_search_per_call_usd),
    ),
    "zep/get_user_name": ModelPricing(
        model_id="zep/get_user_name",
        provider="zep",
        model_type="memory",
        display_name="Zep Get User Name",
        pricing=ExternalPricing(per_call=settings.billing_zep_get_user_name_per_call_usd),
    ),
    "zep/create_user": ModelPricing(
        model_id="zep/create_user",
        provider="zep",
        model_type="memory",
        display_name="Zep Create User",
        pricing=ExternalPricing(per_call=settings.billing_zep_create_user_per_call_usd),
    ),
    "zep/create_thread": ModelPricing(
        model_id="zep/create_thread",
        provider="zep",
        model_type="memory",
        display_name="Zep Create Thread",
        pricing=ExternalPricing(per_call=settings.billing_zep_create_thread_per_call_usd),
    ),
}

# =============================================================================
# Combined pricing dictionary
# =============================================================================

ALL_PRICING: dict[str, ModelPricing] = {
    **LLM_PRICING,
    **STT_PRICING,
    **TTS_PRICING,
    **REALTIME_PRICING,
    **EXTERNAL_PRICING,
}


def calculate_token_cost(
    pricing: TokenPricing,
    prompt_tokens: int,
    completion_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Calculate exact token cost from prompt, completion, and cached tokens."""
    cached_input_tokens = max(cached_input_tokens, 0)
    non_cached_prompt_tokens = max(prompt_tokens - cached_input_tokens, 0)
    prompt_cost = (non_cached_prompt_tokens / 1_000_000) * pricing.input_per_1m
    completion_cost = (completion_tokens / 1_000_000) * pricing.output_per_1m
    cached_cost = 0.0
    if cached_input_tokens and pricing.cached_input_per_1m is not None:
        cached_cost = (cached_input_tokens / 1_000_000) * pricing.cached_input_per_1m
    elif cached_input_tokens:
        cached_cost = (cached_input_tokens / 1_000_000) * pricing.input_per_1m
    return prompt_cost + completion_cost + cached_cost


def calculate_audio_cost(pricing: AudioPricing, units_used: float) -> float:
    """Calculate cost for STT or TTS units."""
    if pricing.per_minute is not None:
        return units_used * pricing.per_minute
    if pricing.per_1m_characters is not None:
        return (units_used / 1_000_000) * pricing.per_1m_characters
    return 0.0


def calculate_realtime_cost(
    pricing: RealtimePricing,
    *,
    audio_input_minutes: float = 0.0,
    audio_output_minutes: float = 0.0,
    text_input_tokens: int = 0,
    text_output_tokens: int = 0,
    fallback_minutes: float = 0.0,
) -> float:
    """Calculate realtime cost from the most detailed units available."""
    has_detailed_audio = audio_input_minutes > 0 or audio_output_minutes > 0
    has_detailed_text = text_input_tokens > 0 or text_output_tokens > 0

    if not has_detailed_audio and not has_detailed_text:
        average_audio = (pricing.audio_input_per_minute + pricing.audio_output_per_minute) / 2
        return fallback_minutes * average_audio

    cost = 0.0
    cost += audio_input_minutes * pricing.audio_input_per_minute
    cost += audio_output_minutes * pricing.audio_output_per_minute
    if pricing.text_input_per_1m is not None:
        cost += (text_input_tokens / 1_000_000) * pricing.text_input_per_1m
    if pricing.text_output_per_1m is not None:
        cost += (text_output_tokens / 1_000_000) * pricing.text_output_per_1m
    return cost


def calculate_external_cost(pricing: ExternalPricing, units_used: float) -> float:
    """Calculate cost for request-based external services."""
    return units_used * pricing.per_call


def get_pricing_by_type(
    model_type: Literal["llm", "stt", "tts", "realtime", "tool", "memory"],
) -> dict[str, ModelPricing]:
    """Get all pricing for a specific model type."""
    match model_type:
        case "llm":
            return LLM_PRICING
        case "stt":
            return STT_PRICING
        case "tts":
            return TTS_PRICING
        case "realtime":
            return REALTIME_PRICING
        case "tool" | "memory":
            return {
                model_id: pricing
                for model_id, pricing in EXTERNAL_PRICING.items()
                if pricing.model_type == model_type
            }


def get_model_pricing(model_id: str) -> ModelPricing | None:
    """Get pricing for a specific model."""
    return ALL_PRICING.get(model_id)
