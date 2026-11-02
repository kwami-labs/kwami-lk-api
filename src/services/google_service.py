"""The user's own Google account: Gmail and Calendar.

Separate from the kwami's mailbox (`email_service`) and calendar
(`calendar_service`), which keep working without any of this.

The refresh token is the key to someone's whole mailbox, so it is encrypted
before it reaches the database and never leaves this module. Text from Google is
returned as data: a message can say anything, and deciding what to do about it
belongs to the user.
"""

from __future__ import annotations

import base64
import logging
import time
from email.message import EmailMessage
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings
from src.services.channels import now_iso
from src.services.credits import get_supabase_admin

logger = logging.getLogger("kwami-api")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 -- an endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary"
SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
)
STATE_TTL_SECONDS = 600
TIMEOUT_SECONDS = 20.0
MAX_BODY_CHARS = 20_000
MAX_LIST_RESULTS = 25
#: Refreshed this long before it expires, so one never dies in the middle of a call.
EXPIRY_MARGIN_SECONDS = 60

_access_tokens: dict[str, tuple[str, float]] = {}


class GoogleNotConfiguredError(RuntimeError):
    """The deployment has no Google client."""


class GoogleNotConnectedError(RuntimeError):
    """The user has not linked an account, or Google no longer honours the link."""


class GoogleRequestError(RuntimeError):
    """Google refused the request. Carries no upstream text."""


def _fernet() -> Fernet:
    if not settings.google_enabled:
        raise GoogleNotConfiguredError("Google is not configured")
    return Fernet(str(settings.google_token_key).encode())


def redirect_uri() -> str:
    return f"{str(settings.app_public_url).rstrip('/')}/google/callback"


def read_state(state: str) -> str:
    """The user this consent was started for. A forged or stale state raises ValueError."""
    try:
        return _fernet().decrypt(state.encode(), ttl=STATE_TTL_SECONDS).decode()
    except InvalidToken as exc:
        raise ValueError("Invalid or expired state") from exc


def authorization_url(user_id: str) -> str:
    query = httpx.QueryParams(
        {
            # `state` first: it is what raises when Google is not configured.
            "state": _fernet().encrypt(user_id.encode()).decode(),
            "client_id": settings.google_oauth_client_id or "",
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # Both are needed for Google to hand out a refresh token every time.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTH_URL}?{query}"


async def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": settings.google_oauth_client_id or "",
                "client_secret": settings.google_oauth_client_secret or "",
                **payload,
            },
        )
    if response.status_code >= 400:
        raise GoogleNotConnectedError("Google refused the token request")
    tokens: dict[str, Any] = response.json()
    return tokens


async def complete_connection(user_id: str, code: str) -> str:
    tokens = await _post_token(
        {"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()}
    )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise GoogleNotConnectedError("Google did not return a refresh token")
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        info = await client.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
    if info.status_code >= 400:
        raise GoogleNotConnectedError("Google did not say whose account this is")
    email = str(info.json().get("email") or "")
    await (
        get_supabase_admin()
        .table("kwami_google_accounts")
        .upsert(
            {
                "user_id": user_id,
                "email": email,
                "refresh_token_encrypted": _fernet().encrypt(refresh_token.encode()).decode(),
                "scopes": str(tokens.get("scope") or ""),
                "updated_at": now_iso(),
            },
            on_conflict="user_id",
        )
        .execute()
    )
    _access_tokens.pop(user_id, None)
    return email


async def _account(user_id: str) -> dict[str, Any] | None:
    result = await (
        get_supabase_admin()
        .table("kwami_google_accounts")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


async def connection_status(user_id: str) -> dict[str, Any]:
    account = await _account(user_id)
    return {
        "configured": settings.google_enabled,
        "connected": bool(account),
        "email": account["email"] if account else None,
    }


async def disconnect(user_id: str) -> bool:
    account = await _account(user_id)
    if not account:
        return False
    # Deleted whatever Google says: the user asked for the link to go away, and a
    # revocation that fails is still something they can finish from their account.
    try:
        token = _fernet().decrypt(account["refresh_token_encrypted"].encode()).decode()
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            await client.post(REVOKE_URL, data={"token": token})
    except (InvalidToken, httpx.HTTPError, GoogleNotConfiguredError):
        logger.warning("Google revocation failed; removing the local link anyway")
    await (
        get_supabase_admin()
        .table("kwami_google_accounts")
        .delete()
        .eq("user_id", user_id)
        .execute()
    )
    _access_tokens.pop(user_id, None)
    return True


async def _access_token(user_id: str) -> str:
    cached = _access_tokens.get(user_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    account = await _account(user_id)
    if not account:
        raise GoogleNotConnectedError("No Google account is linked")
    try:
        refresh_token = _fernet().decrypt(account["refresh_token_encrypted"].encode()).decode()
    except InvalidToken as exc:
        raise GoogleNotConnectedError("The stored Google link cannot be read") from exc
    tokens = await _post_token({"refresh_token": refresh_token, "grant_type": "refresh_token"})
    expires_in = int(tokens.get("expires_in") or 0)
    token = str(tokens["access_token"])
    _access_tokens[user_id] = (token, time.monotonic() + expires_in - EXPIRY_MARGIN_SECONDS)
    return token


async def _google(
    user_id: str,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = await _access_token(user_id)
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.request(
            method, url, params=params, json=body, headers={"Authorization": f"Bearer {token}"}
        )
    if response.status_code == 401:
        _access_tokens.pop(user_id, None)
        raise GoogleNotConnectedError("Google no longer accepts this link")
    if response.status_code >= 400:
        logger.warning("Google answered %s to %s %s", response.status_code, method, url)
        raise GoogleRequestError(f"Google answered {response.status_code}")
    return response.json() if response.content else {}


def _headers(message: dict[str, Any]) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers", [])
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _plain_text(part: dict[str, Any]) -> str:
    """The first text/plain part, depth first. HTML is left out rather than guessed at."""
    data = part.get("body", {}).get("data")
    if part.get("mimeType") == "text/plain" and data:
        return _decode(data)
    for child in part.get("parts", []):
        text = _plain_text(child)
        if text:
            return text
    return ""


def _summary(message: dict[str, Any]) -> dict[str, Any]:
    headers = _headers(message)
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": message.get("snippet", ""),
        "unread": "UNREAD" in message.get("labelIds", []),
    }


async def list_messages(
    user_id: str, query: str = "", max_results: int = 10
) -> list[dict[str, Any]]:
    listing = await _google(
        user_id,
        "GET",
        f"{GMAIL_URL}/messages",
        params={"q": query, "maxResults": min(max_results, MAX_LIST_RESULTS)},
    )
    messages = []
    for item in listing.get("messages", []):
        message = await _google(
            user_id,
            "GET",
            f"{GMAIL_URL}/messages/{item['id']}",
            params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
        )
        messages.append(_summary(message))
    return messages


async def read_message(user_id: str, message_id: str) -> dict[str, Any]:
    message = await _google(
        user_id, "GET", f"{GMAIL_URL}/messages/{message_id}", params={"format": "full"}
    )
    return {**_summary(message), "body": _plain_text(message.get("payload", {}))[:MAX_BODY_CHARS]}


async def send_message(
    user_id: str,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    message = EmailMessage()
    payload: dict[str, Any] = {}
    final_subject = subject
    if reply_to_message_id:
        original = await _google(
            user_id,
            "GET",
            f"{GMAIL_URL}/messages/{reply_to_message_id}",
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "Subject"]},
        )
        headers = _headers(original)
        payload["threadId"] = original.get("threadId")
        original_id = headers.get("message-id", "")
        if original_id:
            message["In-Reply-To"] = original_id
            message["References"] = original_id
        if not final_subject:
            original_subject = headers.get("subject", "")
            final_subject = (
                original_subject
                if original_subject.lower().startswith("re:")
                else f"Re: {original_subject}"
            )
    # EmailMessage refuses a line break in a header with ValueError, which is what
    # stops a recipient or subject from smuggling extra headers in.
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = final_subject
    message.set_content(body)
    payload["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = await _google(user_id, "POST", f"{GMAIL_URL}/messages/send", body=payload)
    return {"id": sent.get("id"), "thread_id": sent.get("threadId")}


async def archive_message(user_id: str, message_id: str) -> None:
    await _google(
        user_id,
        "POST",
        f"{GMAIL_URL}/messages/{message_id}/modify",
        body={"removeLabelIds": ["INBOX"]},
    )


def _event(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id"),
        "title": event.get("summary", ""),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "starts_at": start.get("dateTime") or start.get("date"),
        "ends_at": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start,
        "attendees": [a.get("email") for a in event.get("attendees", [])],
        "link": event.get("htmlLink"),
    }


async def list_events(user_id: str, range_start: str, range_end: str) -> list[dict[str, Any]]:
    listing = await _google(
        user_id,
        "GET",
        f"{CALENDAR_URL}/events",
        params={
            "timeMin": range_start,
            "timeMax": range_end,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        },
    )
    return [_event(item) for item in listing.get("items", [])]


def _event_body(fields: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if fields.get("title") is not None:
        body["summary"] = fields["title"]
    for name in ("description", "location"):
        if fields.get(name) is not None:
            body[name] = fields[name]
    if fields.get("starts_at") is not None:
        body["start"] = {"dateTime": fields["starts_at"]}
    if fields.get("ends_at") is not None:
        body["end"] = {"dateTime": fields["ends_at"]}
    if fields.get("attendees") is not None:
        body["attendees"] = [{"email": email} for email in fields["attendees"]]
    return body


async def create_event(user_id: str, **fields: Any) -> dict[str, Any]:
    created = await _google(
        user_id,
        "POST",
        f"{CALENDAR_URL}/events",
        params={"sendUpdates": "all" if fields.get("attendees") else "none"},
        body=_event_body(fields),
    )
    return _event(created)


async def update_event(user_id: str, event_id: str, **fields: Any) -> dict[str, Any]:
    updated = await _google(
        user_id,
        "PATCH",
        f"{CALENDAR_URL}/events/{event_id}",
        params={"sendUpdates": "all" if fields.get("attendees") else "none"},
        body=_event_body(fields),
    )
    return _event(updated)


async def delete_event(user_id: str, event_id: str) -> None:
    await _google(user_id, "DELETE", f"{CALENDAR_URL}/events/{event_id}")
