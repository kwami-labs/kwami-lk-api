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

The public library (`GET /v1/shared-voices`) is searched and saved into the
account only by an administrator, since the account is the whole deployment's.
There failures do reach the caller, who is waiting for an answer: each has its
own error code. Voices saved from the library do not use the plan's custom voice
slots (ElevenLabs' help centre; on 2026-11-03 this account had 12 designed and 20
library voices with `voice_slots_used` at 12), so a full `voice_limit` does not
stop an add. `max_voice_add_edits` might, and the docs do not say how library
voices count towards it. So nothing is refused in advance: the figures are shown
and ElevenLabs' own answer decides.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal
from urllib.parse import urlencode

import aiohttp
from pydantic import BaseModel

from src.core.config import get_settings
from src.core.errors import (
    ConflictError,
    DomainError,
    ExternalServiceError,
    NotFoundError,
    ServiceUnavailableError,
)
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


# =============================================================================
# The public library
# =============================================================================

LIBRARY_PAGE_SIZE = 30
#: What the two ids of a library voice are made of. They go into ElevenLabs' path with
#: the deployment's key, where "../" would reach another endpoint.
_LIBRARY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class VoiceLibraryNoKeyError(ServiceUnavailableError):
    """This deployment has no ElevenLabs key."""

    code = "voice_library_no_key"


class VoiceLibraryFailedError(ExternalServiceError):
    """ElevenLabs did not answer."""

    code = "voice_library_failed"


class VoiceKeyRefusedError(ExternalServiceError):
    """The ElevenLabs key has no permission to do this."""

    code = "voice_key_refused"


class VoiceNoRoomError(ConflictError):
    """The ElevenLabs account has no room for another voice."""

    code = "voice_no_room"


class VoiceFreePlanError(ConflictError):
    """A free ElevenLabs plan cannot speak with library voices through the API."""

    code = "voice_free_plan"


class VoiceWithdrawnError(NotFoundError):
    """That voice is no longer in the library."""

    code = "voice_withdrawn"


class LibraryVoice(Voice):
    """A voice of the public library, with what it takes to save it."""

    owner_id: str
    #: The name to save it under: `to_voice` splits the tagline off it again.
    full_name: str
    in_account: bool = False


class LibraryPage(BaseModel):
    voices: list[LibraryVoice]
    has_more: bool
    total: int


class LibraryCapacity(BaseModel):
    tier: str | None = None
    #: Designed or cloned voices against the plan's slots.
    voice_slots_used: int = 0
    voice_limit: int = 0
    #: The plan's voice additions and edits, which has no limit on some plans.
    adds_used: int = 0
    adds_limit: int | None = None


def has_key() -> bool:
    return bool(get_settings().elevenlabs_api_key)


def to_library_voice(raw: dict[str, Any]) -> LibraryVoice | None:
    """One `/v1/shared-voices` entry, or None when it cannot be saved."""
    labels = {key: raw.get(key) for key in ("gender", "accent", "language", "descriptive")}
    voice = to_voice({**raw, "labels": labels})
    owner_id = raw.get("public_owner_id")
    if not voice or not owner_id:
        return None
    return LibraryVoice(
        **voice.model_dump(),
        owner_id=owner_id,
        full_name=str(raw.get("name") or voice.id),
        in_account=bool(raw.get("is_added_by_user")),
    )


def _client(api_key: str) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"xi-api-key": api_key}, timeout=TIMEOUT)


async def search_library(
    *,
    search: str | None = None,
    language: str | None = None,
    gender: str | None = None,
    page: int = 0,
) -> LibraryPage:
    """A page of the library, the most used voices first."""
    api_key = get_settings().elevenlabs_api_key
    if not api_key:
        raise VoiceLibraryNoKeyError
    query: dict[str, str | int] = {
        "page_size": LIBRARY_PAGE_SIZE,
        "page": page,
        "sort": "cloned_by_count",
        # A voice with its own rate would bill above the plan on every sentence.
        "include_custom_rates": "false",
    }
    filters = {"search": search, "language": language, "gender": gender}
    query.update({key: value for key, value in filters.items() if value})
    try:
        async with _client(api_key) as session:
            data = await _get_json(session, f"/shared-voices?{urlencode(query)}")
    except aiohttp.ClientResponseError as exc:
        logger.warning("ElevenLabs refused a library search (%s)", exc.status)
        if exc.status in (401, 403):
            raise VoiceKeyRefusedError from exc
        raise VoiceLibraryFailedError from exc
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning("Could not search the ElevenLabs library (%s)", exc)
        raise VoiceLibraryFailedError from exc
    voices = [to_library_voice(raw) for raw in data.get("voices") or []]
    return LibraryPage(
        voices=[voice for voice in voices if voice],
        has_more=bool(data.get("has_more")),
        total=int(data.get("total_count") or 0),
    )


async def _read_capacity(session: aiohttp.ClientSession) -> LibraryCapacity | None:
    try:
        plan = await _get_json(session, "/user/subscription")
    except (aiohttp.ClientError, TimeoutError, ValueError):
        # A key without the user_read permission cannot see its plan.
        logger.info("ElevenLabs plan unknown; its limits cannot be shown")
        return None
    return LibraryCapacity(
        tier=plan.get("tier"),
        voice_slots_used=plan.get("voice_slots_used") or 0,
        voice_limit=plan.get("voice_limit") or 0,
        adds_used=plan.get("voice_add_edit_counter") or 0,
        adds_limit=plan.get("max_voice_add_edits"),
    )


async def library_capacity() -> LibraryCapacity | None:
    """How much room the plan has, or None when it cannot be read."""
    api_key = get_settings().elevenlabs_api_key
    if not api_key:
        return None
    async with _client(api_key) as session:
        return await _read_capacity(session)


async def _post_json(
    session: aiohttp.ClientSession, path: str, payload: dict[str, Any]
) -> tuple[int, Any]:
    async with session.post(f"{API_BASE}{path}", json=payload) as response:
        try:
            return response.status, await response.json(content_type=None)
        except ValueError:
            return response.status, None


def _refusal(status: int, body: Any) -> DomainError:
    """What a refused add means.

    The docs only describe the 422, so the wording decides whether the account is
    full (`voice_limit_reached` is what ElevenLabs is known to answer).
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    said = (
        f"{detail.get('status', '')} {detail.get('message', '')}"
        if isinstance(detail, dict)
        else str(detail or "")
    ).lower()
    logger.warning("ElevenLabs refused to add a voice (%s): %s", status, said[:200])
    if status == 404:
        return VoiceWithdrawnError()
    if (
        status in (400, 402, 403, 409)
        and "voice" in said
        and ("limit" in said or "maximum" in said)
    ):
        return VoiceNoRoomError()
    if status in (401, 403):
        return VoiceKeyRefusedError()
    return VoiceLibraryFailedError()


async def add_library_voice(owner_id: str, voice_id: str, name: str) -> str:
    """Save a library voice into the account; the id it has there."""
    if not (_LIBRARY_ID.fullmatch(owner_id) and _LIBRARY_ID.fullmatch(voice_id)):
        raise VoiceWithdrawnError
    api_key = get_settings().elevenlabs_api_key
    if not api_key:
        raise VoiceLibraryNoKeyError
    try:
        async with _client(api_key) as session:
            capacity = await _read_capacity(session)
            if capacity and capacity.tier == "free":
                # The API would save it, and then the agent could not speak with it.
                raise VoiceFreePlanError
            status, body = await _post_json(
                session, f"/voices/add/{owner_id}/{voice_id}", {"new_name": name}
            )
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("Could not add an ElevenLabs voice (%s)", exc)
        raise VoiceLibraryFailedError from exc
    if status >= 400:
        raise _refusal(status, body)
    clear_cache()
    return (body or {}).get("voice_id") or voice_id
