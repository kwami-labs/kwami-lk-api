"""The ElevenLabs voices this deployment's own account can speak with.

The static list in `config/livekit_voices.yaml` only knows the stock voices, so
the picker never showed the voices the user designed or saved, and several of
its ids had been retired by ElevenLabs. With a key, the list comes from the
account itself (`GET /v1/voices`), each voice with its preview to listen to.

A free plan cannot use library voices through the API: ElevenLabs answers 402
("Free users cannot use library voices via the API"), checked 2026-10-19 with
the agent's key. Offering them would leave the agent mute, so on a free plan
only the stock voices and the account's own (designed or cloned) ones are kept.

Failures never reach the caller: no key, a network error or a bad answer all
mean "use the YAML list".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

import aiohttp

from src.core.config import get_settings
from src.services.voices import Voice

logger = logging.getLogger("kwami-api.voices.elevenlabs")

API_BASE = "https://api.elevenlabs.io/v1"

#: Seconds a good answer is reused, and how soon a failed one is retried.
TTL_OK = 600.0
TTL_FAILED = 60.0
TIMEOUT = aiohttp.ClientTimeout(total=8)

#: Categories a free plan can use through the API: stock and the account's own.
FREE_PLAN_CATEGORIES = frozenset({"premade", "generated", "cloned"})
#: The account's own voices, grouped apart from the stock ones in the picker.
OWN_CATEGORIES = frozenset({"generated", "cloned"})

_cache: tuple[float, list[Voice] | None] | None = None


def _gender(labels: dict[str, Any]) -> Literal["male", "female", "neutral"] | None:
    value = str(labels.get("gender") or "").lower()
    if value in ("male", "female", "neutral"):
        return value  # type: ignore[return-value]
    return None


def to_voice(raw: dict[str, Any]) -> Voice | None:
    """One `/v1/voices` entry as a catalogue voice, or None when it has no id."""
    voice_id = raw.get("voice_id")
    if not voice_id:
        return None
    labels = raw.get("labels") or {}
    # Stock names read "George - Warm, Captivating Storyteller".
    full_name = str(raw.get("name") or voice_id)
    name, _, tagline = full_name.partition(" - ")
    gender = _gender(labels)
    if raw.get("category") in OWN_CATEGORIES:
        category = "Custom"
    else:
        category = {"male": "Male", "female": "Female", "neutral": "Neutral"}.get(
            gender or "", "Other"
        )
    accent = labels.get("accent")
    description = tagline.strip() or labels.get("descriptive") or None
    if accent and description:
        description = f"{description} ({accent})"
    elif accent:
        description = str(accent)
    return Voice(
        id=voice_id,
        name=name.strip() or full_name,
        category=category,
        gender=gender,
        language=labels.get("language") or None,
        description=description,
        preview_url=raw.get("preview_url") or None,
    )


def usable_on(tier: str | None, raw: dict[str, Any]) -> bool:
    """Whether the API lets this plan speak with the voice.

    An unknown plan is treated as free: hiding a voice is better than offering
    one the agent then cannot use.
    """
    if tier and tier != "free":
        return True
    return raw.get("category") in FREE_PLAN_CATEGORIES


async def _get_json(session: aiohttp.ClientSession, path: str) -> Any:
    async with session.get(f"{API_BASE}{path}") as response:
        response.raise_for_status()
        return await response.json()


async def _fetch(api_key: str) -> list[Voice]:
    headers = {"xi-api-key": api_key}
    async with aiohttp.ClientSession(headers=headers, timeout=TIMEOUT) as session:
        data = await _get_json(session, "/voices")
        try:
            tier = (await _get_json(session, "/user/subscription")).get("tier")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            # A key without the user_read permission cannot see its plan.
            logger.info("ElevenLabs plan unknown; keeping the voices a free plan can use")
            tier = None
    voices = []
    for raw in data.get("voices") or []:
        if not usable_on(tier, raw):
            continue
        voice = to_voice(raw)
        if voice:
            voices.append(voice)
    # The account's own voices first, then by name.
    voices.sort(key=lambda v: (v.category != "Custom", v.name.lower()))
    return voices


async def account_voices() -> list[Voice] | None:
    """The account's usable voices, or None to fall back to the YAML list."""
    global _cache
    now = time.monotonic()
    if _cache and _cache[0] > now:
        return _cache[1]
    api_key = get_settings().elevenlabs_api_key
    if not api_key:
        return None
    try:
        voices: list[Voice] | None = await _fetch(api_key)
        _cache = (now + TTL_OK, voices or None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning("Could not list ElevenLabs voices (%s); using the YAML list", exc)
        voices = None
        _cache = (now + TTL_FAILED, None)
    return voices if voices else None


def clear_cache() -> None:
    global _cache
    _cache = None
