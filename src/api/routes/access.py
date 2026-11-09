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
    """Show the site access list.

    Requires a user JWT. Returns the default level for every other site, the sites with their levels,
    the requests from Jarvis still waiting for an answer, and the valid `levels`.
    """
    return await access.list_access(user.id)


@router.get("/check")
async def check_site(
    user: Annotated[AuthUser, Depends(require_auth)],
    address: Annotated[str, Query(min_length=1, max_length=access.MAX_ADDRESS_CHARS)],
) -> dict[str, str]:
    """Say what Jarvis may do at an address.

    Requires a user JWT. Returns the normalised `host`, the `access` level that applies and the
    `pattern` that decides it (the rule for every other site when none matches).
    Returns 400 when `address` is not a site address.
    """
    try:
        return await access.check(user.id, address)
    except InvalidSiteError as exc:
        raise _bad(exc) from exc


@router.put("/default")
async def set_default(
    body: LevelBody, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, str]:
    """Set the access level for every site without a rule of its own.

    Requires a user JWT. Returns `{"default": level}`. Returns 400 for an unknown level.
    """
    try:
        return {"default": await access.set_default(user.id, body.access)}
    except UnknownLevelError as exc:
        raise _bad(exc) from exc


@router.put("/sites")
async def set_site(
    body: SiteBody, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    """Set the access level for one site, replacing any it had.

    Requires a user JWT. Returns the stored rule. Errors: 400 for an unknown level or an address that is
    not a site, 409 when the list of sites is full (200 sites).
    """
    try:
        return await access.set_site(user.id, body.address, body.access)
    except (InvalidSiteError, TooManySitesError, UnknownLevelError) as exc:
        raise _bad(exc) from exc


@router.delete("/sites/{rule_id}", status_code=204)
async def remove_site(rule_id: UUID, user: Annotated[AuthUser, Depends(require_auth)]) -> None:
    """Forget a site rule.

    Requires a user JWT. Returns 204 with no body. The rule for every other site is never removed here.
    Returns 404 when there is no such site.
    """
    if not await access.remove_site(user.id, str(rule_id)):
        raise HTTPException(status_code=404, detail="No such site")


@router.post("/requests/{rule_id}")
async def answer_request(
    rule_id: UUID, body: AnswerBody, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    """Approve or refuse a request from Jarvis to visit a site.

    Requires a user JWT. Approving keeps the rule at the level asked for, or at `access` when given, and
    returns it as `site`. Refusing forgets the request and returns `site` as null.
    Errors: 400 for an unknown level, 404 when approving a request that does not exist.
    """
    try:
        rule = await access.answer_request(user.id, str(rule_id), body.approve, body.access)
    except UnknownLevelError as exc:
        raise _bad(exc) from exc
    if body.approve and rule is None:
        raise HTTPException(status_code=404, detail="No such request")
    return {"site": rule}
