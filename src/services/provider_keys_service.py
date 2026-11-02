"""The model keys a user connects from the app, so the brain runs on their account.

A key is checked against its provider, encrypted before it reaches the database,
and never leaves here again: the app gets the last characters to recognise it by,
and only the agent, which holds the same secret, can open it. Nothing a provider
answers is passed on or logged, and neither is the key.

Most providers have no way to sign in from another application, so "connect" means
the app opens the provider's page for making a key and the user pastes it here.
OpenRouter does have one: consent on its site hands back a key for this user alone.

A Claude subscription is deliberately not here. Anthropic does not allow an
application to offer Claude login or to hold those credentials. Someone who wants
to use their plan signs in to Claude Code on their own PC and runs the work thread
there (`agent/src/brain/worker.py`), where the credentials never leave the machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings
from src.services.channels import now_iso
from src.services.credits import get_supabase_admin

logger = logging.getLogger("kwami-api")

TABLE = "kwami_provider_keys"
#: Under the app's 15 s: it must hear the answer, not give up before it comes.
TIMEOUT_SECONDS = 10.0
STATE_TTL_SECONDS = 600
MIN_KEY_CHARS = 8
HINT_CHARS = 4

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"


class ProviderKeysNotConfiguredError(RuntimeError):
    """The deployment has no secret to encrypt keys with."""


class UnknownProviderError(ValueError):
    """Not a provider a key can be connected for."""


class InvalidKeyError(ValueError):
    """The provider says this key is no good, or it does not look like one."""


class ProviderUnavailableError(RuntimeError):
    """The provider could not say either way. Carries no upstream text."""


@dataclass(frozen=True)
class Check:
    """A cheap, free call that succeeds only with a valid key."""

    url: str
    header: str = "Authorization"
    prefix: str = "Bearer "


CHECKS: dict[str, Check] = {
    "anthropic": Check("https://api.anthropic.com/v1/models", "x-api-key", ""),
    "google": Check(
        "https://generativelanguage.googleapis.com/v1beta/models", "x-goog-api-key", ""
    ),
    "openai": Check("https://api.openai.com/v1/models"),
    "xai": Check("https://api.x.ai/v1/models"),
    # Its model list is public: this one is the call that needs a key.
    "openrouter": Check("https://openrouter.ai/api/v1/key"),
    "groq": Check("https://api.groq.com/openai/v1/models"),
    "deepseek": Check("https://api.deepseek.com/models"),
    "mistral": Check("https://api.mistral.ai/v1/models"),
}


def _fernet() -> Fernet:
    secret = settings.provider_keys_secret
    if not secret:
        raise ProviderKeysNotConfiguredError("PROVIDER_KEYS_SECRET is not set")
    return Fernet(secret.encode())


def _hint(key: str) -> str:
    return key[-HINT_CHARS:]


async def list_connections(user_id: str) -> dict[str, Any]:
    """What this user has connected, without the keys. Works unconfigured: nothing is."""
    enabled = bool(settings.provider_keys_secret)
    rows: list[Any] = []
    if enabled:
        result = await (
            get_supabase_admin()
            .table(TABLE)
            .select("provider, key_hint, updated_at")
            .eq("user_id", user_id)
            .execute()
        )
        rows = result.data or []
    return {
        "enabled": enabled,
        "providers": sorted(CHECKS),
        "connected": {
            str(row["provider"]): {"hint": row["key_hint"], "updatedAt": row["updated_at"]}
            for row in rows
        },
    }


async def _verify(provider: str, key: str) -> None:
    check = CHECKS[provider]
    headers = {check.header: f"{check.prefix}{key}"}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(check.url, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(f"{provider} did not answer") from exc
    if response.status_code in (400, 401, 403):
        raise InvalidKeyError("The provider refused this key")
    if response.status_code >= 300:
        raise ProviderUnavailableError(f"{provider} answered {response.status_code}")


async def _store(user_id: str, provider: str, key: str) -> None:
    await (
        get_supabase_admin()
        .table(TABLE)
        .upsert(
            {
                "user_id": user_id,
                "provider": provider,
                "encrypted_key": _fernet().encrypt(key.encode()).decode(),
                "key_hint": _hint(key),
                "updated_at": now_iso(),
            },
            on_conflict="user_id,provider",
        )
        .execute()
    )


async def connect_key(user_id: str, provider: str, key: str) -> dict[str, str]:
    """Check a pasted key and keep it. Returns what the app may show."""
    _fernet()  # before anything goes out: no point checking what cannot be kept
    if provider not in CHECKS:
        raise UnknownProviderError(provider)
    key = key.strip()
    if len(key) < MIN_KEY_CHARS or any(c.isspace() for c in key):
        raise InvalidKeyError("This does not look like a key")
    await _verify(provider, key)
    await _store(user_id, provider, key)
    return {"hint": _hint(key)}


async def disconnect(user_id: str, provider: str) -> bool:
    """Forget the key. Revoking it is up to the user, on the provider's site."""
    result = await (
        get_supabase_admin()
        .table(TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("provider", provider)
        .execute()
    )
    return bool(result.data)


def _callback_url(state: str) -> str:
    base = str(settings.app_public_url).rstrip("/")
    return f"{base}/providers/openrouter/callback?{httpx.QueryParams({'state': state})}"


def openrouter_authorization_url(user_id: str) -> str:
    """Where to send the user to approve a key.

    OpenRouter returns to the callback with only a code, so what the callback needs
    (whose it is, the PKCE verifier) travels encrypted inside the callback's own URL.
    """
    if not settings.app_public_url:
        raise ProviderKeysNotConfiguredError("APP_PUBLIC_URL is not set")
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = _fernet().encrypt(json.dumps({"u": user_id, "v": verifier}).encode()).decode()
    query = httpx.QueryParams(
        {
            "callback_url": _callback_url(state),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{OPENROUTER_AUTH_URL}?{query}"


def _read_state(state: str) -> tuple[str, str]:
    try:
        data = json.loads(_fernet().decrypt(state.encode(), ttl=STATE_TTL_SECONDS))
    except (InvalidToken, ValueError) as exc:
        raise InvalidKeyError("Invalid or expired state") from exc
    return str(data["u"]), str(data["v"])


async def complete_openrouter(state: str, code: str) -> None:
    """Swap the code for a key. A forged or stale state raises `InvalidKeyError`."""
    user_id, verifier = _read_state(state)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                OPENROUTER_KEYS_URL,
                json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
            )
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError("OpenRouter did not answer") from exc
    if response.status_code >= 300:
        raise InvalidKeyError("OpenRouter refused the code")
    key = response.json().get("key")
    if not isinstance(key, str) or not key:
        raise InvalidKeyError("OpenRouter returned no key")
    await _store(user_id, "openrouter", key)
