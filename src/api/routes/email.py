"""Email Smart Hub API routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.config import settings
from src.core.security import AuthUser
from src.services import email_service
from src.services.sendgrid_service import send_email

router = APIRouter()
logger = logging.getLogger("kwami-api.email")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CheckUsernameRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=30)


class CheckUsernameResponse(BaseModel):
    available: bool
    error: str | None = None


class ActivateRequest(BaseModel):
    kwami_id: str
    username: str = Field(..., min_length=3, max_length=30)


class AccountResponse(BaseModel):
    id: str
    username: str
    email_address: str
    is_active: bool


class SendEmailRequest(BaseModel):
    kwami_id: str
    to_addresses: list[str]
    cc_addresses: list[str] = []
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    reply_to_message_id: str | None = None


class MessagePatchRequest(BaseModel):
    is_read: bool | None = None
    is_starred: bool | None = None
    is_archived: bool | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/check-username", response_model=CheckUsernameResponse)
async def check_username(
    body: CheckUsernameRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    err = email_service.validate_username(body.username)
    if err:
        return CheckUsernameResponse(available=False, error=err)
    available = await email_service.check_username_available(body.username)
    return CheckUsernameResponse(available=available)


@router.post("/activate", response_model=AccountResponse)
async def activate_email(
    body: ActivateRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    try:
        account = await email_service.activate_account(
            user_id=user.id,
            kwami_id=body.kwami_id,
            username=body.username,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AccountResponse(
        id=account["id"],
        username=account["username"],
        email_address=account["email_address"],
        is_active=account["is_active"],
    )


@router.get("/account")
async def get_account(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str = Query(...),
):
    account = await email_service.get_account(user.id, kwami_id)
    if not account:
        return {"account": None}
    return {
        "account": {
            "id": account["id"],
            "username": account["username"],
            "email_address": account["email_address"],
            "is_active": account["is_active"],
        }
    }


@router.delete("/account")
async def deactivate_account(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str = Query(...),
):
    removed = await email_service.deactivate_account(user.id, kwami_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No email account found")
    return {"ok": True}


@router.get("/inbox")
async def get_inbox(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str = Query(...),
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
):
    messages = await email_service.fetch_inbox(
        user.id,
        kwami_id,
        category=category,
        page=page,
        page_size=page_size,
    )
    return {"messages": messages}


@router.get("/unread-counts")
async def get_unread_counts(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: str = Query(...),
):
    counts = await email_service.get_unread_counts(user.id, kwami_id)
    return {"counts": counts}


@router.get("/messages/{message_id}")
async def get_message(
    message_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    msg = await email_service.get_message(user.id, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"message": msg}


@router.patch("/messages/{message_id}")
async def update_message(
    message_id: str,
    body: MessagePatchRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    fields: dict[str, Any] = {}
    if body.is_read is not None:
        fields["is_read"] = body.is_read
    if body.is_starred is not None:
        fields["is_starred"] = body.is_starred
    if body.is_archived is not None:
        fields["is_archived"] = body.is_archived

    msg = await email_service.update_message(user.id, message_id, **fields)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"message": msg}


@router.post("/send")
async def send_email_route(
    body: SendEmailRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    if not settings.email_enabled:
        raise HTTPException(status_code=503, detail="Email sending is not configured")

    account = await email_service.get_account(user.id, body.kwami_id)
    if not account:
        raise HTTPException(status_code=400, detail="Email account not activated")

    message_id = await send_email(
        from_address=account["email_address"],
        to_addresses=body.to_addresses,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        cc_addresses=body.cc_addresses or None,
    )

    stored = await email_service.store_outbound_email(
        account=account,
        to_addresses=body.to_addresses,
        cc_addresses=body.cc_addresses,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        sendgrid_message_id=message_id,
    )

    return {"ok": True, "message": stored}
