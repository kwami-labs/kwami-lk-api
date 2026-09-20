"""Calendar event CRUD API routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.security import AuthUser
from src.services import calendar_service

router = APIRouter()


class CalendarEventCreateRequest(BaseModel):
    kwami_id: str
    title: str = Field(..., min_length=1, max_length=200)
    starts_at: str
    ends_at: str
    description: str = ""
    all_day: bool = False
    event_type: str = "other"
    color: str = "#6366f1"
    location: str = ""
    metadata: dict[str, Any] = {}


class CalendarEventUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: str | None = None
    ends_at: str | None = None
    description: str | None = None
    all_day: bool | None = None
    event_type: str | None = None
    color: str | None = None
    location: str | None = None
    metadata: dict[str, Any] | None = None


@router.get("/events")
async def get_events(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str = Query(...),
    range_start: str = Query(...),
    range_end: str = Query(...),
):
    try:
        events = await calendar_service.list_events(user.id, kwami_id, range_start, range_end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"events": events}


@router.post("/events")
async def create_event(
    body: CalendarEventCreateRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    try:
        event = await calendar_service.create_event(
            user.id,
            body.kwami_id,
            title=body.title,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            description=body.description,
            all_day=body.all_day,
            event_type=body.event_type,
            color=body.color,
            location=body.location,
            metadata=body.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"event": event}


@router.patch("/events/{event_id}")
async def patch_event(
    event_id: str,
    body: CalendarEventUpdateRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    try:
        event = await calendar_service.update_event(
            user.id,
            event_id,
            title=body.title,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            description=body.description,
            all_day=body.all_day,
            event_type=body.event_type,
            color=body.color,
            location=body.location,
            metadata=body.metadata,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail == "Event not found" else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    return {"event": event}


@router.delete("/events/{event_id}")
async def remove_event(
    event_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    deleted = await calendar_service.delete_event(user.id, event_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True}
