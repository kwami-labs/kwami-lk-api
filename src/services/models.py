"""Model metadata configuration.

This file contains detailed metadata for AI models used with LiveKit.
Metadata includes context windows, capabilities, speed tiers, and more.

Update this file when new models are added or capabilities change.
Last updated: 2026-01-28
"""

from typing import Literal

from pydantic import BaseModel


class ModelMetadata(BaseModel):
    """Complete metadata for a model."""

    model_id: str
    display_name: str
    provider: str
    context_window: int  # Max tokens in context
    max_output: int | None = None  # Max output tokens (None = same as context)
    capabilities: list[str] = []  # vision, function_calling, json_mode, streaming
    speed: Literal["fast", "standard", "slow"] = "standard"
    tier: Literal["flagship", "standard", "budget"] = "standard"
    description: str | None = None


# =============================================================================
# LLM Models Metadata
# =============================================================================

LLM_METADATA: dict[str, ModelMetadata] = {
    # -------------------------------------------------------------------------
    # OpenAI Models (Inference)
    # -------------------------------------------------------------------------
    "openai/gpt-5": ModelMetadata(
        model_id="openai/gpt-5",
        display_name="GPT-5",
        provider="openai",
        context_window=256000,
        max_output=32768,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="standard",
        tier="flagship",
    ),
    "openai/gpt-5-mini": ModelMetadata(
        model_id="openai/gpt-5-mini",
        display_name="GPT-5 Mini",
        provider="openai",
        context_window=256000,
        max_output=32768,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
    ),
    "openai/gpt-5-nano": ModelMetadata(
        model_id="openai/gpt-5-nano",
        display_name="GPT-5 Nano",
        provider="openai",
        context_window=128000,
        max_output=16384,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "openai/gpt-4.1": ModelMetadata(
        model_id="openai/gpt-4.1",
        display_name="GPT-4.1",
        provider="openai",
        context_window=1000000,
        max_output=32768,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="standard",
        tier="flagship",
    ),
    "openai/gpt-4.1-mini": ModelMetadata(
        model_id="openai/gpt-4.1-mini",
        display_name="GPT-4.1 Mini",
        provider="openai",
        context_window=1000000,
        max_output=32768,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
    ),
    "openai/gpt-4.1-nano": ModelMetadata(
        model_id="openai/gpt-4.1-nano",
        display_name="GPT-4.1 Nano",
        provider="openai",
        context_window=1000000,
        max_output=32768,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "openai/gpt-4o": ModelMetadata(
        model_id="openai/gpt-4o",
        display_name="GPT-4o",
        provider="openai",
        context_window=128000,
        max_output=16384,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="flagship",
    ),
    "openai/gpt-4o-mini": ModelMetadata(
        model_id="openai/gpt-4o-mini",
        display_name="GPT-4o Mini",
        provider="openai",
        context_window=128000,
        max_output=16384,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    # -------------------------------------------------------------------------
    # OpenAI Models (Plugin)
    # -------------------------------------------------------------------------
    "gpt-4o": ModelMetadata(
        model_id="gpt-4o",
        display_name="GPT-4o",
        provider="openai",
        context_window=128000,
        max_output=16384,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="flagship",
    ),
    "gpt-4o-mini": ModelMetadata(
        model_id="gpt-4o-mini",
        display_name="GPT-4o Mini",
        provider="openai",
        context_window=128000,
        max_output=16384,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "gpt-4-turbo": ModelMetadata(
        model_id="gpt-4-turbo",
        display_name="GPT-4 Turbo",
        provider="openai",
        context_window=128000,
        max_output=4096,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="standard",
        tier="flagship",
    ),
    "gpt-4": ModelMetadata(
        model_id="gpt-4",
        display_name="GPT-4",
        provider="openai",
        context_window=8192,
        max_output=4096,
        capabilities=["function_calling", "streaming"],
        speed="slow",
        tier="standard",
    ),
    "gpt-3.5-turbo": ModelMetadata(
        model_id="gpt-3.5-turbo",
        display_name="GPT-3.5 Turbo",
        provider="openai",
        context_window=16385,
        max_output=4096,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "o1": ModelMetadata(
        model_id="o1",
        display_name="o1",
        provider="openai",
        context_window=200000,
        max_output=100000,
        capabilities=["vision", "function_calling", "streaming"],
        speed="slow",
        tier="flagship",
        description="Advanced reasoning model",
    ),
    "o1-mini": ModelMetadata(
        model_id="o1-mini",
        display_name="o1 Mini",
        provider="openai",
        context_window=128000,
        max_output=65536,
        capabilities=["streaming"],
        speed="standard",
        tier="standard",
        description="Fast reasoning model",
    ),
    "o1-preview": ModelMetadata(
        model_id="o1-preview",
        display_name="o1 Preview",
        provider="openai",
        context_window=128000,
        max_output=32768,
        capabilities=["streaming"],
        speed="slow",
        tier="flagship",
    ),
    # -------------------------------------------------------------------------
    # Google Models (Inference)
    # -------------------------------------------------------------------------
    "google/gemini-2.5-pro": ModelMetadata(
        model_id="google/gemini-2.5-pro",
        display_name="Gemini 2.5 Pro",
        provider="google",
        context_window=1000000,
        max_output=65536,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="standard",
        tier="flagship",
    ),
    "google/gemini-2.5-flash": ModelMetadata(
        model_id="google/gemini-2.5-flash",
        display_name="Gemini 2.5 Flash",
        provider="google",
        context_window=1000000,
        max_output=65536,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
    ),
    "google/gemini-2.5-flash-lite": ModelMetadata(
        model_id="google/gemini-2.5-flash-lite",
        display_name="Gemini 2.5 Flash Lite",
        provider="google",
        context_window=1000000,
        max_output=65536,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "google/gemini-2.0-flash": ModelMetadata(
        model_id="google/gemini-2.0-flash",
        display_name="Gemini 2.0 Flash",
        provider="google",
        context_window=1000000,
        max_output=8192,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
    ),
    "google/gemini-2.0-flash-lite": ModelMetadata(
        model_id="google/gemini-2.0-flash-lite",
        display_name="Gemini 2.0 Flash Lite",
        provider="google",
        context_window=1000000,
        max_output=8192,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    # -------------------------------------------------------------------------
    # Google Models (Plugin)
    # -------------------------------------------------------------------------
    "gemini-2.0-flash": ModelMetadata(
        model_id="gemini-2.0-flash",
        display_name="Gemini 2.0 Flash",
        provider="google",
        context_window=1000000,
        max_output=8192,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
    ),
    "gemini-2.0-flash-lite": ModelMetadata(
        model_id="gemini-2.0-flash-lite",
        display_name="Gemini 2.0 Flash Lite",
        provider="google",
        context_window=1000000,
        max_output=8192,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "gemini-1.5-flash": ModelMetadata(
        model_id="gemini-1.5-flash",
        display_name="Gemini 1.5 Flash",
        provider="google",
        context_window=1000000,
        max_output=8192,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
    ),
    "gemini-1.5-pro": ModelMetadata(
        model_id="gemini-1.5-pro",
        display_name="Gemini 1.5 Pro",
        provider="google",
        context_window=2000000,
        max_output=8192,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="standard",
        tier="flagship",
    ),
    "gemini-1.0-pro": ModelMetadata(
        model_id="gemini-1.0-pro",
        display_name="Gemini 1.0 Pro",
        provider="google",
        context_window=32760,
        max_output=8192,
        capabilities=["function_calling", "streaming"],
        speed="fast",
        tier="budget",
    ),
    # -------------------------------------------------------------------------
    # Anthropic Models (Plugin)
    # -------------------------------------------------------------------------
    "claude-sonnet-4-20250514": ModelMetadata(
        model_id="claude-sonnet-4-20250514",
        display_name="Claude Sonnet 4",
        provider="anthropic",
        context_window=200000,
        max_output=64000,
        capabilities=["vision", "function_calling", "streaming"],
        speed="fast",
        tier="flagship",
    ),
    "claude-3-5-sonnet-latest": ModelMetadata(
        model_id="claude-3-5-sonnet-latest",
        display_name="Claude 3.5 Sonnet",
        provider="anthropic",
        context_window=200000,
        max_output=8192,
        capabilities=["vision", "function_calling", "streaming"],
        speed="fast",
        tier="flagship",
    ),
    "claude-3-5-sonnet-20241022": ModelMetadata(
        model_id="claude-3-5-sonnet-20241022",
        display_name="Claude 3.5 Sonnet (Oct)",
        provider="anthropic",
        context_window=200000,
        max_output=8192,
        capabilities=["vision", "function_calling", "streaming"],
        speed="fast",
        tier="flagship",
    ),
    "claude-3-5-haiku-latest": ModelMetadata(
        model_id="claude-3-5-haiku-latest",
        display_name="Claude 3.5 Haiku",
        provider="anthropic",
        context_window=200000,
        max_output=8192,
        capabilities=["vision", "function_calling", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "claude-3-opus-latest": ModelMetadata(
        model_id="claude-3-opus-latest",
        display_name="Claude 3 Opus",
        provider="anthropic",
        context_window=200000,
        max_output=4096,
        capabilities=["vision", "function_calling", "streaming"],
        speed="slow",
        tier="flagship",
    ),
    "claude-3-haiku-20240307": ModelMetadata(
        model_id="claude-3-haiku-20240307",
        display_name="Claude 3 Haiku",
        provider="anthropic",
        context_window=200000,
        max_output=4096,
        capabilities=["vision", "function_calling", "streaming"],
        speed="fast",
        tier="budget",
    ),
    # -------------------------------------------------------------------------
    # DeepSeek Models
    # -------------------------------------------------------------------------
    "deepseek-ai/deepseek-v3": ModelMetadata(
        model_id="deepseek-ai/deepseek-v3",
        display_name="DeepSeek V3",
        provider="deepseek",
        context_window=128000,
        max_output=8192,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    # -------------------------------------------------------------------------
    # Groq Models (Plugin)
    # -------------------------------------------------------------------------
    "llama-3.3-70b-versatile": ModelMetadata(
        model_id="llama-3.3-70b-versatile",
        display_name="Llama 3.3 70B",
        provider="groq",
        context_window=128000,
        max_output=32768,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
        description="Fastest inference via Groq",
    ),
    "llama-3.1-8b-instant": ModelMetadata(
        model_id="llama-3.1-8b-instant",
        display_name="Llama 3.1 8B",
        provider="groq",
        context_window=128000,
        max_output=8192,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "mixtral-8x7b-32768": ModelMetadata(
        model_id="mixtral-8x7b-32768",
        display_name="Mixtral 8x7B",
        provider="groq",
        context_window=32768,
        max_output=32768,
        capabilities=["function_calling", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "gemma2-9b-it": ModelMetadata(
        model_id="gemma2-9b-it",
        display_name="Gemma 2 9B",
        provider="groq",
        context_window=8192,
        max_output=8192,
        capabilities=["streaming"],
        speed="fast",
        tier="budget",
    ),
    # -------------------------------------------------------------------------
    # Mistral Models (Plugin)
    # -------------------------------------------------------------------------
    "mistral-large-latest": ModelMetadata(
        model_id="mistral-large-latest",
        display_name="Mistral Large",
        provider="mistralai",
        context_window=128000,
        max_output=8192,
        capabilities=["vision", "function_calling", "json_mode", "streaming"],
        speed="standard",
        tier="flagship",
    ),
    "mistral-medium-latest": ModelMetadata(
        model_id="mistral-medium-latest",
        display_name="Mistral Medium",
        provider="mistralai",
        context_window=128000,
        max_output=8192,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="standard",
    ),
    "mistral-small-latest": ModelMetadata(
        model_id="mistral-small-latest",
        display_name="Mistral Small",
        provider="mistralai",
        context_window=128000,
        max_output=8192,
        capabilities=["function_calling", "json_mode", "streaming"],
        speed="fast",
        tier="budget",
    ),
    "open-mistral-nemo": ModelMetadata(
        model_id="open-mistral-nemo",
        display_name="Mistral Nemo",
        provider="mistralai",
        context_window=128000,
        max_output=8192,
        capabilities=["function_calling", "streaming"],
        speed="fast",
        tier="budget",
    ),
    # -------------------------------------------------------------------------
    # Kimi Models
    # -------------------------------------------------------------------------
    "moonshotai/kimi-k2-instruct": ModelMetadata(
        model_id="moonshotai/kimi-k2-instruct",
        display_name="Kimi K2",
        provider="moonshot",
        context_window=128000,
        max_output=8192,
        capabilities=["function_calling", "streaming"],
        speed="fast",
        tier="standard",
    ),
}


def get_model_metadata(model_id: str) -> ModelMetadata | None:
    """Get metadata for a specific model."""
    return LLM_METADATA.get(model_id)


def get_default_metadata(model_id: str, provider: str) -> ModelMetadata:
    """Generate default metadata for unknown models."""
    # Try to infer from model name
    name = model_id.split("/")[-1] if "/" in model_id else model_id
    display_name = name.replace("-", " ").replace("_", " ").title()

    # Infer capabilities from common patterns
    capabilities = ["streaming"]
    if any(x in model_id.lower() for x in ["gpt-4", "claude-3", "gemini", "llama-3"]):
        capabilities.append("function_calling")
    if any(x in model_id.lower() for x in ["4o", "gemini-2", "claude-3-5", "claude-sonnet"]):
        capabilities.append("vision")

    # Infer speed from common patterns
    speed: Literal["fast", "standard", "slow"] = "standard"
    if any(x in model_id.lower() for x in ["mini", "nano", "flash", "haiku", "instant", "turbo"]):
        speed = "fast"
    elif any(x in model_id.lower() for x in ["opus", "pro", "large"]):
        speed = "slow"

    # Infer tier
    tier: Literal["flagship", "standard", "budget"] = "standard"
    if any(x in model_id.lower() for x in ["opus", "pro", "large", "flagship"]):
        tier = "flagship"
    elif any(x in model_id.lower() for x in ["mini", "nano", "lite", "haiku", "small", "budget"]):
        tier = "budget"

    return ModelMetadata(
        model_id=model_id,
        display_name=display_name,
        provider=provider,
        context_window=128000,  # Safe default
        max_output=8192,
        capabilities=capabilities,
        speed=speed,
        tier=tier,
    )
