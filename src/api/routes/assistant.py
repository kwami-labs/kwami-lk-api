"""Reminders, follow-ups, report settings, notifications and the send record."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.api.ratelimit import ASSISTANT_SETTINGS_LIMIT, NOTIFY_CODE_LIMIT, limiter
from src.core.security import AuthUser
from src.services import assistant_scheduler, assistant_service

router = APIRouter()


class SettingsRequest(BaseModel):
    kwami_id: str
    notify_to: str | None = None
    notify_channel: str = "whatsapp"
    timezone: str = assistant_service.DEFAULT_TIMEZONE
    reports_enabled: bool = False
    notify_code: str = Field(default="", max_length=16)
    notify_proof: str = Field(default="", max_length=128)


class NotifyCodeRequest(BaseModel):
    kwami_id: str
    notify_to: str = Field(..., min_length=1, max_length=32)
    notify_channel: str = "whatsapp"


class TaskCreateRequest(BaseModel):
    kwami_id: str
    kind: str = "reminder"
    title: str = Field(..., min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)
    run_at: str | None = None
    recurrence: str | None = None
    weekdays: list[int] | None = None
    local_time: str | None = None
    timezone: str | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    status: str | None = None


class ExpressSendRequest(BaseModel):
    kwami_id: str
    channel: str = Field(..., min_length=1, max_length=32)
    recipient: str = Field(..., min_length=1, max_length=320)
    summary: str = ""
    express_order: str = Field(..., min_length=1)


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/settings")
async def read_settings(
    user: Annotated[AuthUser, Depends(require_auth)], kwami_id: str = Query(...)
) -> dict[str, Any]:
    try:
        return {"settings": await assistant_service.get_settings(user.id, kwami_id)}
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/settings/notify-code")
@limiter.limit(NOTIFY_CODE_LIMIT)
async def send_notify_code(
    request: Request, body: NotifyCodeRequest, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, str]:
    """Send a number the code that confirms it. The proof comes back when saving."""
    try:
        await assistant_service.get_owned_kwami(user.id, body.kwami_id)
        proof = await assistant_scheduler.send_notify_code(
            user.id, body.kwami_id, body.notify_to, body.notify_channel
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="The code could not be sent") from exc
    return {"proof": proof}


@router.put("/settings")
@limiter.limit(ASSISTANT_SETTINGS_LIMIT)
async def write_settings(
    request: Request, body: SettingsRequest, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        row = await assistant_service.save_settings(
            user.id,
            body.kwami_id,
            notify_to=body.notify_to,
            notify_channel=body.notify_channel,
            timezone=body.timezone,
            reports_enabled=body.reports_enabled,
            notify_code=body.notify_code,
            notify_proof=body.notify_proof,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"settings": row}


@router.get("/tasks")
async def read_tasks(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str = Query(...),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return {"tasks": await assistant_service.list_tasks(user.id, kwami_id, status=status)}
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/tasks")
async def add_task(
    body: TaskCreateRequest, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    fields = body.model_dump(exclude={"kwami_id"})
    try:
        return {"task": await assistant_service.create_task(user.id, body.kwami_id, **fields)}
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.patch("/tasks/{task_id}")
async def change_task(
    task_id: str, body: TaskUpdateRequest, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        return {
            "task": await assistant_service.update_task(
                user.id, task_id, title=body.title, notes=body.notes, status=body.status
            )
        }
    except ValueError as exc:
        status = 404 if str(exc) == "Task not found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.delete("/tasks/{task_id}")
async def remove_task(
    task_id: str, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, bool]:
    if not await assistant_service.delete_task(user.id, task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@router.get("/notifications")
async def read_notifications(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str = Query(...),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    try:
        rows = await assistant_service.list_notifications(
            user.id, kwami_id, unread_only=unread_only, limit=limit
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"notifications": rows}


@router.post("/notifications/{notification_id}/read")
async def read_notification(
    notification_id: str, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, bool]:
    if not await assistant_service.mark_notification_read(user.id, notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"ok": True}


@router.post("/express-sends")
async def record_express_send(
    body: ExpressSendRequest, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        row = await assistant_service.record_express_send(
            user.id,
            body.kwami_id,
            channel=body.channel,
            recipient=body.recipient,
            summary=body.summary,
            express_order=body.express_order,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"record": row}
