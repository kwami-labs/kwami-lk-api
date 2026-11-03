"""The user's own Google accounts: linking them, Gmail and Calendar.

Every call takes `account`, the address of the link to use. Without it, the oldest.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.api.ratelimit import PROVISIONING_LIMIT, limiter
from src.core.security import AuthUser
from src.services import google_service, google_worker, google_workspace, oauth_return
from src.services.google_service import (
    GoogleNotConfiguredError,
    GoogleNotConnectedError,
    GoogleRequestError,
)
from src.services.oauth_return import UnknownReturnError

router = APIRouter()

T = TypeVar("T")
Account = Annotated[str | None, Query(max_length=320)]

_PAGE = (
    "<!doctype html><html lang=es><meta charset=utf-8><title>Kwami</title>"
    "<body style='font-family:system-ui;text-align:center;padding:4rem;"
    "background:#0b0d12;color:#e8eaf0'>"
    "<h1>{heading}</h1><p>{text}</p></body></html>"
)


class ConnectRequest(BaseModel):
    desktop: bool = False


class CompleteRequest(BaseModel):
    state: str = Field(..., min_length=1, max_length=4096)
    code: str = Field(..., min_length=1, max_length=4096)


class WorkThreadCall(BaseModel):
    """One action of the work thread in the user's Google accounts."""

    action: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)


class SendRequest(BaseModel):
    to: list[str] = Field(..., min_length=1, max_length=50)
    cc: list[str] | None = None
    subject: str = Field(default="", max_length=500)
    body: str = Field(..., min_length=1)
    reply_to_message_id: str | None = None
    attachment_file_ids: list[str] | None = Field(default=None, max_length=10)


class LabelCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=225)


class LabelModifyRequest(BaseModel):
    add: list[str] | None = None
    remove: list[str] | None = None


class FilterCreateRequest(BaseModel):
    criteria: dict[str, Any]
    action: dict[str, Any]


class EventCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    starts_at: str
    ends_at: str
    description: str | None = None
    location: str | None = None
    attendees: list[str] | None = None


class EventUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    starts_at: str | None = None
    ends_at: str | None = None
    description: str | None = None
    location: str | None = None
    attendees: list[str] | None = None


async def _attachments(
    user: AuthUser, body: SendRequest, account: str | None
) -> list[google_service.Attachment] | None:
    if not body.attachment_file_ids:
        return None
    return await _call(
        google_workspace.drive_attachments(user.id, body.attachment_file_ids, account=account)
    )


async def _call(call: Awaitable[T]) -> T:
    try:
        return await call
    except GoogleNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Google is not configured") from exc
    except GoogleNotConnectedError as exc:
        raise HTTPException(status_code=409, detail="Google account not connected") from exc
    except GoogleRequestError as exc:
        raise HTTPException(status_code=502, detail="Google rejected the request") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/status")
async def status(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    return await google_service.connection_status(user.id)


@router.post("/connect")
async def connect(
    request: Request,
    user: Annotated[AuthUser, Depends(require_auth)],
    body: ConnectRequest = ConnectRequest(),  # noqa: B008
) -> dict[str, str]:
    try:
        target = oauth_return.return_target(request.headers.get("origin"), desktop=body.desktop)
        return {"url": google_service.authorization_url(user.id, target)}
    except UnknownReturnError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GoogleNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Google is not configured") from exc


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    state: str = Query(default=""), code: str = Query(default=""), error: str = Query(default="")
) -> Response:
    """Where Google sends the browser. It connects nothing: see `oauth_return`."""
    try:
        if error or not code:
            raise ValueError("Consent not given")
        target = google_service.read_state(state).target
    except (ValueError, GoogleNotConfiguredError):
        return HTMLResponse(
            _PAGE.format(
                heading="No se ha conectado", text="Vuelva a la app e inténtelo de nuevo."
            ),
            status_code=400,
        )
    return RedirectResponse(oauth_return.return_url(target, "google", state, code), 302)


@router.post("/complete")
@limiter.limit(PROVISIONING_LIMIT)
async def complete(
    request: Request,
    body: CompleteRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    """The app, signed in, finishes what the callback passed on."""
    try:
        started_by = google_service.read_state(body.state).user_id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GoogleNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Google is not configured") from exc
    if started_by != user.id:
        raise HTTPException(status_code=403, detail="Another user started this")
    try:
        return {"email": await google_service.complete_connection(user.id, body.code)}
    except GoogleNotConnectedError as exc:
        raise HTTPException(status_code=400, detail="Google refused this") from exc


@router.post("/work-thread/token")
async def work_thread_token(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    """For the work thread on this user's PC. It opens their Google and nobody else's."""
    try:
        token = google_service.work_thread_token(user.id)
    except GoogleNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Google is not configured") from exc
    return {"token": token, "expires_in": google_service.WORK_TOKEN_TTL_SECONDS}


@router.post("/work-thread")
async def work_thread(
    body: WorkThreadCall,
    x_kwami_work_token: Annotated[str, Header(alias="X-Kwami-Work-Token")] = "",
) -> dict[str, Any]:
    """The work thread's calls. The user comes from the token, never from the caller."""
    try:
        user_id = google_service.read_work_thread_token(x_kwami_work_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GoogleNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Google is not configured") from exc
    return {"result": await _call(google_worker.call(user_id, body.action, body.args))}


@router.delete("/account")
async def disconnect(
    user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, bool]:
    if not await google_service.disconnect(user.id, account):
        raise HTTPException(status_code=404, detail="No Google account is linked")
    return {"ok": True}


@router.get("/gmail/messages")
async def gmail_list(
    user: Annotated[AuthUser, Depends(require_auth)],
    q: str = Query(default=""),
    max_results: int = Query(default=10, ge=1, le=25),
    account: Account = None,
) -> dict[str, Any]:
    messages = await _call(google_service.list_messages(user.id, q, max_results, account=account))
    return {"messages": messages}


@router.get("/gmail/messages/{message_id}")
async def gmail_read(
    message_id: str, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, Any]:
    return {
        "message": await _call(google_service.read_message(user.id, message_id, account=account))
    }


@router.post("/gmail/send")
async def gmail_send(
    body: SendRequest, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, Any]:
    sent = await _call(
        google_service.send_message(
            user.id,
            to=body.to,
            cc=body.cc,
            subject=body.subject,
            body=body.body,
            reply_to_message_id=body.reply_to_message_id,
            account=account,
            attachments=await _attachments(user, body, account),
        )
    )
    return {"message": sent}


@router.post("/gmail/drafts")
async def gmail_draft_create(
    body: SendRequest, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, Any]:
    draft = await _call(
        google_service.create_draft(
            user.id,
            to=body.to,
            cc=body.cc,
            subject=body.subject,
            body=body.body,
            reply_to_message_id=body.reply_to_message_id,
            account=account,
            attachments=await _attachments(user, body, account),
        )
    )
    return {"draft": draft}


@router.get("/gmail/drafts")
async def gmail_draft_list(
    user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, Any]:
    return {"drafts": await _call(google_service.list_drafts(user.id, account=account))}


@router.post("/gmail/drafts/{draft_id}/send")
async def gmail_draft_send(
    draft_id: str, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, Any]:
    return {"message": await _call(google_service.send_draft(user.id, draft_id, account=account))}


@router.get("/gmail/messages/{message_id}/attachments/{attachment_id}")
async def gmail_attachment(
    message_id: str,
    attachment_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    account: Account = None,
) -> dict[str, Any]:
    found = await _call(
        google_service.read_attachment(user.id, message_id, attachment_id, account=account)
    )
    return {"attachment": found}


@router.get("/gmail/labels")
async def gmail_labels(
    user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, Any]:
    return {"labels": await _call(google_service.list_labels(user.id, account=account))}


@router.post("/gmail/labels")
async def gmail_label_create(
    body: LabelCreateRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    account: Account = None,
) -> dict[str, Any]:
    return {"label": await _call(google_service.create_label(user.id, body.name, account=account))}


@router.post("/gmail/messages/{message_id}/labels")
async def gmail_label_modify(
    message_id: str,
    body: LabelModifyRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    account: Account = None,
) -> dict[str, bool]:
    await _call(
        google_service.modify_labels(
            user.id, message_id, add=body.add, remove=body.remove, account=account
        )
    )
    return {"ok": True}


@router.post("/gmail/messages/{message_id}/trash")
async def gmail_trash(
    message_id: str, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, bool]:
    await _call(google_service.trash_message(user.id, message_id, account=account))
    return {"ok": True}


@router.post("/gmail/messages/{message_id}/untrash")
async def gmail_untrash(
    message_id: str, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, bool]:
    await _call(google_service.untrash_message(user.id, message_id, account=account))
    return {"ok": True}


@router.get("/gmail/filters")
async def gmail_filters(
    user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, Any]:
    return {"filters": await _call(google_service.list_filters(user.id, account=account))}


@router.post("/gmail/filters")
async def gmail_filter_create(
    body: FilterCreateRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    account: Account = None,
) -> dict[str, Any]:
    created = await _call(
        google_service.create_filter(
            user.id, criteria=body.criteria, action=body.action, account=account
        )
    )
    return {"filter": created}


@router.delete("/gmail/filters/{filter_id}")
async def gmail_filter_delete(
    filter_id: str, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, bool]:
    await _call(google_service.delete_filter(user.id, filter_id, account=account))
    return {"ok": True}


@router.post("/gmail/messages/{message_id}/archive")
async def gmail_archive(
    message_id: str, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, bool]:
    await _call(google_service.archive_message(user.id, message_id, account=account))
    return {"ok": True}


@router.get("/calendar/events")
async def calendar_list(
    user: Annotated[AuthUser, Depends(require_auth)],
    range_start: str = Query(...),
    range_end: str = Query(...),
    account: Account = None,
) -> dict[str, Any]:
    events = await _call(
        google_service.list_events(user.id, range_start, range_end, account=account)
    )
    return {"events": events}


@router.post("/calendar/events")
async def calendar_create(
    body: EventCreateRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    account: Account = None,
) -> dict[str, Any]:
    event = await _call(google_service.create_event(user.id, account=account, **body.model_dump()))
    return {"event": event}


@router.patch("/calendar/events/{event_id}")
async def calendar_update(
    event_id: str,
    body: EventUpdateRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
    account: Account = None,
) -> dict[str, Any]:
    event = await _call(
        google_service.update_event(user.id, event_id, account=account, **body.model_dump())
    )
    return {"event": event}


@router.delete("/calendar/events/{event_id}")
async def calendar_delete(
    event_id: str, user: Annotated[AuthUser, Depends(require_auth)], account: Account = None
) -> dict[str, bool]:
    await _call(google_service.delete_event(user.id, event_id, account=account))
    return {"ok": True}
