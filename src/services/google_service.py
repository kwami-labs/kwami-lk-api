"""The user's own Google accounts: the link, Gmail and Calendar.

A user can link several, and a call names the address it means; with none named it
goes to the oldest link. Drive, Docs, Contacts and Tasks are in `google_workspace`.

Separate from the kwami's mailbox (`email_service`) and calendar
(`calendar_service`), which keep working without any of this.

The refresh token is the key to someone's whole mailbox, so it is encrypted
before it reaches the database and never leaves this module. Text from Google is
returned as data: a message can say anything, and deciding what to do about it
belongs to the user.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
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
    # Not the full `mail.google.com` scope: gmail.modify cannot delete a message for
    # good, so the mailbox has no irreversible path even if a call asks for one.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/tasks",
)
#: Only to learn whose account it is. Google answers "email" with another name for it,
#: so they say nothing about what a link can do.
IDENTITY_SCOPES = frozenset({"openid", "email"})
STATE_TTL_SECONDS = 600
#: A work thread's token cannot be taken back, so it runs out.
WORK_TOKEN_TTL_SECONDS = 90 * 24 * 3600
_WORK_TOKEN_PURPOSE = "work-thread"  # noqa: S105
TIMEOUT_SECONDS = 60.0
MAX_BODY_CHARS = 20_000
MAX_LIST_RESULTS = 25
#: What Gmail accepts in one message.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
#: Refreshed this long before it expires, so one never dies in the middle of a call.
EXPIRY_MARGIN_SECONDS = 60

#: What Gmail and Calendar ids are made of. One comes from the caller and goes into the
#: path, where "../" would reach another Google endpoint with the user's own token.
_GOOGLE_ID = re.compile(r"[A-Za-z0-9_-]{1,1024}")

#: Keyed by user and the address asked for, "" for the default link.
_access_tokens: dict[tuple[str, str], tuple[str, float]] = {}


@dataclass(frozen=True)
class Attachment:
    filename: str
    mime_type: str
    data: bytes


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
    base = settings.google_redirect_base_url or settings.app_public_url
    return f"{str(base).rstrip('/')}/google/callback"


@dataclass(frozen=True)
class ConsentState:
    """Who started a consent, and where its result goes (`oauth_return`)."""

    user_id: str
    target: str


def read_state(state: str) -> ConsentState:
    """What this consent was started with. A forged or stale state raises ValueError."""
    try:
        data = json.loads(_fernet().decrypt(state.encode(), ttl=STATE_TTL_SECONDS))
        return ConsentState(user_id=str(data["u"]), target=str(data["r"]))
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid or expired state") from exc


def work_thread_token(user_id: str) -> str:
    """What the work thread on a PC shows: it opens this user's Google and nobody else's."""
    payload = json.dumps({"p": _WORK_TOKEN_PURPOSE, "u": user_id})
    return _fernet().encrypt(payload.encode()).decode()


def read_work_thread_token(token: str) -> str:
    """The user it belongs to. Anything else encrypted with this key raises ValueError."""
    try:
        data = json.loads(_fernet().decrypt(token.encode(), ttl=WORK_TOKEN_TTL_SECONDS))
        if data["p"] != _WORK_TOKEN_PURPOSE:
            raise ValueError("Not a work thread token")
        return str(data["u"])
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid or expired token") from exc


def authorization_url(user_id: str, target: str) -> str:
    query = httpx.QueryParams(
        {
            # `state` first: it is what raises when Google is not configured.
            "state": _fernet().encrypt(json.dumps({"u": user_id, "r": target}).encode()).decode(),
            "client_id": settings.google_oauth_client_id or "",
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # Both are needed for Google to hand out a refresh token every time.
            "access_type": "offline",
            # select_account: without it Google reuses the signed-in account, and a
            # second one could never be linked.
            "prompt": "select_account consent",
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
    if not email:
        raise GoogleNotConnectedError("Google did not say whose account this is")
    await (
        get_supabase_admin()
        .table("kwami_google_accounts")
        .upsert(
            {
                "user_id": user_id,
                "email": email,
                "refresh_token_encrypted": _fernet().encrypt(refresh_token.encode()).decode(),
                "scopes": str(tokens.get("scope") or ""),
                "needs_reconnect_at": None,
                "updated_at": now_iso(),
            },
            on_conflict="user_id,email",
        )
        .execute()
    )
    _forget_tokens(user_id)
    return email


def _forget_tokens(user_id: str) -> None:
    for key in [key for key in _access_tokens if key[0] == user_id]:
        del _access_tokens[key]


async def _accounts(user_id: str) -> list[dict[str, Any]]:
    result = await (
        get_supabase_admin()
        .table("kwami_google_accounts")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    )
    return getattr(result, "data", None) or []


async def _account(user_id: str, account: str | None = None) -> dict[str, Any] | None:
    rows = await _accounts(user_id)
    if account:
        wanted = account.strip().lower()
        return next((row for row in rows if str(row["email"]).lower() == wanted), None)
    return rows[0] if rows else None


async def connection_status(user_id: str) -> dict[str, Any]:
    rows = await _accounts(user_id)
    return {
        "configured": settings.google_enabled,
        "connected": bool(rows),
        "email": rows[0]["email"] if rows else None,
        "accounts": [
            {
                "email": row["email"],
                "needs_reconnect": bool(row.get("needs_reconnect_at")),
                # Linked before a permission was added: it works, but not for that.
                "missing_scopes": sorted(
                    (set(SCOPES) - IDENTITY_SCOPES) - set(str(row.get("scopes", "")).split())
                ),
            }
            for row in rows
        ],
    }


async def disconnect(user_id: str, account: str | None = None) -> bool:
    row = await _account(user_id, account)
    if not row:
        return False
    # Deleted whatever Google says: the user asked for the link to go away, and a
    # revocation that fails is still something they can finish from their account.
    try:
        token = _fernet().decrypt(row["refresh_token_encrypted"].encode()).decode()
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            await client.post(REVOKE_URL, data={"token": token})
    except (InvalidToken, httpx.HTTPError, GoogleNotConfiguredError):
        logger.warning("Google revocation failed; removing the local link anyway")
    await (
        get_supabase_admin()
        .table("kwami_google_accounts")
        .delete()
        .eq("user_id", user_id)
        .eq("email", row["email"])
        .execute()
    )
    _forget_tokens(user_id)
    return True


async def _access_token(user_id: str, account: str | None = None) -> str:
    key = (user_id, (account or "").strip().lower())
    cached = _access_tokens.get(key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    row = await _account(user_id, account)
    if not row:
        raise GoogleNotConnectedError("No Google account is linked")
    try:
        refresh_token = _fernet().decrypt(row["refresh_token_encrypted"].encode()).decode()
    except InvalidToken as exc:
        raise GoogleNotConnectedError("The stored Google link cannot be read") from exc
    try:
        tokens = await _post_token({"refresh_token": refresh_token, "grant_type": "refresh_token"})
    except GoogleNotConnectedError:
        await (
            get_supabase_admin()
            .table("kwami_google_accounts")
            .update({"needs_reconnect_at": now_iso()})
            .eq("user_id", user_id)
            .eq("email", row["email"])
            .execute()
        )
        raise
    expires_in = int(tokens.get("expires_in") or 0)
    token = str(tokens["access_token"])
    _access_tokens[key] = (token, time.monotonic() + expires_in - EXPIRY_MARGIN_SECONDS)
    return token


def path_id(value: str) -> str:
    if not _GOOGLE_ID.fullmatch(value):
        raise GoogleRequestError("Not a Google id")
    return value


async def _send(
    user_id: str,
    method: str,
    url: str,
    *,
    account: str | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    content: bytes | None = None,
    content_type: str | None = None,
) -> httpx.Response:
    token = await _access_token(user_id, account)
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.request(
            method, url, params=params, json=body, content=content, headers=headers
        )
    if response.status_code == 401:
        _forget_tokens(user_id)
        raise GoogleNotConnectedError("Google no longer accepts this link")
    if response.status_code >= 400:
        logger.warning("Google answered %s to a %s", response.status_code, method)
        raise GoogleRequestError(f"Google answered {response.status_code}")
    return response


async def google_json(user_id: str, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """One call to a Google API. `account` picks the link."""
    response = await _send(user_id, method, url, **kwargs)
    return response.json() if response.content else {}


async def google_bytes(user_id: str, url: str, **kwargs: Any) -> bytes:
    """A file as it is stored."""
    response = await _send(user_id, "GET", url, **kwargs)
    return response.content


async def google_text(user_id: str, url: str, **kwargs: Any) -> str:
    """A body that is a document rather than JSON, cut to what a model can read."""
    response = await _send(user_id, "GET", url, **kwargs)
    return response.text[:MAX_BODY_CHARS]


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


def _attachments(part: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    if part.get("filename") and part.get("body", {}).get("attachmentId"):
        found.append(
            {
                "id": part["body"]["attachmentId"],
                "filename": part["filename"],
                "mime_type": part.get("mimeType", ""),
                "size": part["body"].get("size", 0),
            }
        )
    for child in part.get("parts", []):
        found.extend(_attachments(child))
    return found


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
        "labels": message.get("labelIds", []),
    }


async def list_messages(
    user_id: str, query: str = "", max_results: int = 10, *, account: str | None = None
) -> list[dict[str, Any]]:
    listing = await google_json(
        user_id,
        "GET",
        f"{GMAIL_URL}/messages",
        params={"q": query, "maxResults": min(max_results, MAX_LIST_RESULTS)},
        account=account,
    )
    messages = []
    for item in listing.get("messages", []):
        message = await google_json(
            user_id,
            "GET",
            f"{GMAIL_URL}/messages/{path_id(item['id'])}",
            params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
            account=account,
        )
        messages.append(_summary(message))
    return messages


async def read_message(
    user_id: str, message_id: str, *, account: str | None = None
) -> dict[str, Any]:
    message = await google_json(
        user_id,
        "GET",
        f"{GMAIL_URL}/messages/{path_id(message_id)}",
        params={"format": "full"},
        account=account,
    )
    payload = message.get("payload", {})
    return {
        **_summary(message),
        "body": _plain_text(payload)[:MAX_BODY_CHARS],
        "attachments": _attachments(payload),
    }


async def _compose(
    user_id: str,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
    reply_to_message_id: str | None,
    account: str | None,
    attachments: list[Attachment] | None = None,
) -> dict[str, Any]:
    """The Gmail `raw` payload shared by a sent message and a draft."""
    message = EmailMessage()
    payload: dict[str, Any] = {}
    final_subject = subject
    if reply_to_message_id:
        original = await google_json(
            user_id,
            "GET",
            f"{GMAIL_URL}/messages/{path_id(reply_to_message_id)}",
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "Subject"]},
            account=account,
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
    for attachment in attachments or []:
        maintype, _, subtype = attachment.mime_type.partition("/")
        message.add_attachment(
            attachment.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    payload["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return payload


async def send_message(
    user_id: str,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    reply_to_message_id: str | None = None,
    account: str | None = None,
    attachments: list[Attachment] | None = None,
) -> dict[str, Any]:
    payload = await _compose(
        user_id,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        reply_to_message_id=reply_to_message_id,
        account=account,
        attachments=attachments,
    )
    sent = await google_json(
        user_id, "POST", f"{GMAIL_URL}/messages/send", body=payload, account=account
    )
    return {"id": sent.get("id"), "thread_id": sent.get("threadId")}


async def create_draft(
    user_id: str,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    reply_to_message_id: str | None = None,
    account: str | None = None,
    attachments: list[Attachment] | None = None,
) -> dict[str, Any]:
    payload = await _compose(
        user_id,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        reply_to_message_id=reply_to_message_id,
        account=account,
        attachments=attachments,
    )
    draft = await google_json(
        user_id, "POST", f"{GMAIL_URL}/drafts", body={"message": payload}, account=account
    )
    return {"id": draft.get("id")}


async def list_drafts(user_id: str, *, account: str | None = None) -> list[dict[str, Any]]:
    listing = await google_json(
        user_id,
        "GET",
        f"{GMAIL_URL}/drafts",
        params={"maxResults": MAX_LIST_RESULTS},
        account=account,
    )
    drafts = []
    for item in listing.get("drafts", []):
        draft = await google_json(
            user_id,
            "GET",
            f"{GMAIL_URL}/drafts/{path_id(item['id'])}",
            params={"format": "metadata"},
            account=account,
        )
        drafts.append({**_summary(draft.get("message", {})), "id": item["id"]})
    return drafts


async def send_draft(user_id: str, draft_id: str, *, account: str | None = None) -> dict[str, Any]:
    sent = await google_json(
        user_id, "POST", f"{GMAIL_URL}/drafts/send", body={"id": draft_id}, account=account
    )
    return {"id": sent.get("id"), "thread_id": sent.get("threadId")}


async def attachment_bytes(
    user_id: str, message_id: str, attachment_id: str, *, account: str | None = None
) -> bytes:
    found = await google_json(
        user_id,
        "GET",
        f"{GMAIL_URL}/messages/{path_id(message_id)}/attachments/{path_id(attachment_id)}",
        account=account,
    )
    data = str(found.get("data", ""))
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


async def read_attachment(
    user_id: str, message_id: str, attachment_id: str, *, account: str | None = None
) -> dict[str, Any]:
    """The text of an attachment, or only its size when it is not text.

    A PDF or an image: save it to Drive with `google_workspace.save_attachment`, and
    reading it there gives its text.
    """
    raw = await attachment_bytes(user_id, message_id, attachment_id, account=account)
    try:
        text: str | None = raw.decode("utf-8")[:MAX_BODY_CHARS]
    except UnicodeDecodeError:
        text = None
    return {"size": len(raw), "text": text}


async def list_labels(user_id: str, *, account: str | None = None) -> list[dict[str, Any]]:
    listing = await google_json(user_id, "GET", f"{GMAIL_URL}/labels", account=account)
    return [
        {"id": label.get("id"), "name": label.get("name"), "type": label.get("type")}
        for label in listing.get("labels", [])
    ]


async def create_label(user_id: str, name: str, *, account: str | None = None) -> dict[str, Any]:
    label = await google_json(
        user_id, "POST", f"{GMAIL_URL}/labels", body={"name": name}, account=account
    )
    return {"id": label.get("id"), "name": label.get("name")}


async def modify_labels(
    user_id: str,
    message_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    account: str | None = None,
) -> None:
    await google_json(
        user_id,
        "POST",
        f"{GMAIL_URL}/messages/{path_id(message_id)}/modify",
        body={"addLabelIds": add or [], "removeLabelIds": remove or []},
        account=account,
    )


async def trash_message(user_id: str, message_id: str, *, account: str | None = None) -> None:
    """To the bin, where Gmail keeps it for 30 days. There is no deleting for good here."""
    await google_json(
        user_id, "POST", f"{GMAIL_URL}/messages/{path_id(message_id)}/trash", account=account
    )


async def untrash_message(user_id: str, message_id: str, *, account: str | None = None) -> None:
    await google_json(
        user_id, "POST", f"{GMAIL_URL}/messages/{path_id(message_id)}/untrash", account=account
    )


async def list_filters(user_id: str, *, account: str | None = None) -> list[dict[str, Any]]:
    listing = await google_json(user_id, "GET", f"{GMAIL_URL}/settings/filters", account=account)
    return list(listing.get("filter", []))


async def create_filter(
    user_id: str,
    *,
    criteria: dict[str, Any],
    action: dict[str, Any],
    account: str | None = None,
) -> dict[str, Any]:
    # A filter that forwards would keep sending the user's mail somewhere for good,
    # and one line in a message could be what asked for it.
    if action.get("forward"):
        raise ValueError("A filter cannot forward mail")
    return await google_json(
        user_id,
        "POST",
        f"{GMAIL_URL}/settings/filters",
        body={"criteria": criteria, "action": action},
        account=account,
    )


async def delete_filter(user_id: str, filter_id: str, *, account: str | None = None) -> None:
    await google_json(
        user_id, "DELETE", f"{GMAIL_URL}/settings/filters/{path_id(filter_id)}", account=account
    )


async def archive_message(user_id: str, message_id: str, *, account: str | None = None) -> None:
    await google_json(
        user_id,
        "POST",
        f"{GMAIL_URL}/messages/{path_id(message_id)}/modify",
        body={"removeLabelIds": ["INBOX"]},
        account=account,
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


async def list_events(
    user_id: str, range_start: str, range_end: str, *, account: str | None = None
) -> list[dict[str, Any]]:
    listing = await google_json(
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
        account=account,
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


async def create_event(
    user_id: str, *, account: str | None = None, **fields: Any
) -> dict[str, Any]:
    created = await google_json(
        user_id,
        "POST",
        f"{CALENDAR_URL}/events",
        params={"sendUpdates": "all" if fields.get("attendees") else "none"},
        body=_event_body(fields),
        account=account,
    )
    return _event(created)


async def update_event(
    user_id: str, event_id: str, *, account: str | None = None, **fields: Any
) -> dict[str, Any]:
    updated = await google_json(
        user_id,
        "PATCH",
        f"{CALENDAR_URL}/events/{path_id(event_id)}",
        params={"sendUpdates": "all" if fields.get("attendees") else "none"},
        body=_event_body(fields),
        account=account,
    )
    return _event(updated)


async def delete_event(user_id: str, event_id: str, *, account: str | None = None) -> None:
    await google_json(
        user_id, "DELETE", f"{CALENDAR_URL}/events/{path_id(event_id)}", account=account
    )
