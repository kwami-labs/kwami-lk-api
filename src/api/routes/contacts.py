"""Per-kwami contacts CRUD API routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.config import settings
from src.core.security import AuthUser
from src.services.channels import (
    create_contact,
    delete_contact,
    get_contact,
    get_owned_kwami,
    list_contacts_for_kwami,
    maybe_normalize_phone_number,
    normalize_phone_number,
    update_contact,
)

router = APIRouter()


class ContactUpsertRequest(BaseModel):
    kwami_id: str = Field(alias="kwamiId")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    phone_number: str = Field(alias="phoneNumber")
    whatsapp_address: str | None = Field(default=None, alias="whatsappAddress")
    email: str | None = None
    instagram: str | None = None
    tiktok: str | None = None
    metadata: dict[str, Any] | None = None


@router.get("")
async def list_contacts(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: Annotated[str, Query(alias="kwamiId")],
    q: Annotated[str | None, Query(alias="q")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    await get_owned_kwami(user.id, kwami_id)
    return {
        "contacts": list_contacts_for_kwami(user.id, kwami_id, query=q, limit=limit),
    }


@router.post("")
async def create_contact_route(
    body: ContactUpsertRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    await get_owned_kwami(user.id, body.kwami_id)
    try:
        phone_number = normalize_phone_number(body.phone_number, settings.twilio_phone_country)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    contact = await create_contact(
        user_id=user.id,
        kwami_id=body.kwami_id,
        display_name=body.display_name.strip(),
        phone_number=phone_number,
        whatsapp_address=maybe_normalize_phone_number(
            body.whatsapp_address, settings.twilio_phone_country
        )
        if body.whatsapp_address
        else None,
        email=body.email.strip().lower() if body.email and body.email.strip() else None,
        instagram=body.instagram.strip() if body.instagram and body.instagram.strip() else None,
        tiktok=body.tiktok.strip() if body.tiktok and body.tiktok.strip() else None,
        metadata=body.metadata or {},
    )
    return {"contact": contact}


@router.patch("/{contact_id}")
async def update_contact_route(
    contact_id: str,
    body: ContactUpsertRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    existing = await get_contact(user.id, contact_id)
    if existing["kwami_id"] != body.kwami_id:
        raise HTTPException(status_code=400, detail="Contact does not belong to this kwami")
    updates: dict[str, Any] = {
        "display_name": body.display_name.strip(),
        "phone_number": normalize_phone_number(body.phone_number, settings.twilio_phone_country),
        "whatsapp_address": maybe_normalize_phone_number(
            body.whatsapp_address, settings.twilio_phone_country
        )
        if body.whatsapp_address
        else None,
        "email": body.email.strip().lower() if body.email and body.email.strip() else None,
        "instagram": body.instagram.strip() if body.instagram and body.instagram.strip() else None,
        "tiktok": body.tiktok.strip() if body.tiktok and body.tiktok.strip() else None,
        "metadata": body.metadata or {},
    }
    updated = await update_contact(user_id=user.id, contact_id=contact_id, updates=updates)
    return {"contact": updated}


@router.delete("/{contact_id}")
async def delete_contact_route(
    contact_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: Annotated[str, Query(alias="kwamiId")],
):
    await get_owned_kwami(user.id, kwami_id)
    existing = await get_contact(user.id, contact_id)
    if existing["kwami_id"] != kwami_id:
        raise HTTPException(status_code=400, detail="Contact does not belong to this kwami")
    await delete_contact(user.id, contact_id)
    return {"ok": True}
