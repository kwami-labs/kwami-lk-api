"""`/access`: the sites Jarvis may visit, what it may do on each, and its requests."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.security import AuthUser
from src.services import site_access_service as access
from src.services.site_access_service import (
    InvalidSiteError,
    TooManySitesError,
    UnknownLevelError,
)

router = APIRouter()


class SiteBody(BaseModel):
    address: str = Field(..., min_length=1, max_length=access.MAX_ADDRESS_CHARS)
    access: str


class LevelBody(BaseModel):
    access: str


class AnswerBody(BaseModel):
    approve: bool
    access: str | None = None


def _bad(exc: ValueError) -> HTTPException:
    if isinstance(exc, UnknownLevelError):
        return HTTPException(status_code=400, detail="Unknown access level")
    if isinstance(exc, TooManySitesError):
        return HTTPException(status_code=409, detail="The list of sites is full")
    return HTTPException(status_code=400, detail="That is not a site address")


@router.get("")
async def read_access(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    return await access.list_access(user.id)


@router.get("/check")
async def check_site(
    user: Annotated[AuthUser, Depends(require_auth)],
    address: Annotated[str, Query(min_length=1, max_length=access.MAX_ADDRESS_CHARS)],
) -> dict[str, str]:
    try:
        return await access.check(user.id, address)
    except InvalidSiteError as exc:
        raise _bad(exc) from exc


@router.put("/default")
async def set_default(
    body: LevelBody, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, str]:
    try:
        return {"default": await access.set_default(user.id, body.access)}
    except UnknownLevelError as exc:
        raise _bad(exc) from exc


@router.put("/sites")
async def set_site(
    body: SiteBody, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        return await access.set_site(user.id, body.address, body.access)
    except (InvalidSiteError, TooManySitesError, UnknownLevelError) as exc:
        raise _bad(exc) from exc


@router.delete("/sites/{rule_id}", status_code=204)
async def remove_site(rule_id: UUID, user: Annotated[AuthUser, Depends(require_auth)]) -> None:
    if not await access.remove_site(user.id, str(rule_id)):
        raise HTTPException(status_code=404, detail="No such site")


@router.post("/requests/{rule_id}")
async def answer_request(
    rule_id: UUID, body: AnswerBody, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        rule = await access.answer_request(user.id, str(rule_id), body.approve, body.access)
    except UnknownLevelError as exc:
        raise _bad(exc) from exc
    if body.approve and rule is None:
        raise HTTPException(status_code=404, detail="No such request")
    return {"site": rule}
