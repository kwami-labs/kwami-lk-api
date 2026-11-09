"""`/pc`: the user's own PC, seen and signed in from the app."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.security import AuthUser
from src.services import pc_service

router = APIRouter()


class ClaudeOrder(BaseModel):
    kwami_id: str


class SiteOrder(BaseModel):
    kwami_id: str
    address: str = Field(..., min_length=1, max_length=pc_service.MAX_URL_LENGTH)


@router.get("/status")
async def read_status(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, bool]:
    """Say whether the user's PC runner is online.

    Requires a user JWT. Returns `online` and whether Claude is logged in on it (`claude_login`).
    """
    return await pc_service.status(user.id)


@router.post("/claude")
async def connect_claude(
    body: ClaudeOrder, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, str]:
    """Ask the user's PC to start the Claude account login.

    Requires a user JWT. Returns the `id` of the order; follow it with `GET /pc/orders/{order_id}`.
    Errors: 400 for a kwami that is not the user's, 409 when the PC is not online.
    """
    try:
        return {"id": await pc_service.connect_claude(user.id, body.kwami_id)}
    except pc_service.PcOfflineError as exc:
        raise HTTPException(status_code=409, detail="The PC is not there") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sites")
async def show_site(
    body: SiteOrder, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, str]:
    """Ask the user's PC to open a site so the user can sign in to it.

    Requires a user JWT. Records the site as pending in the user's list and returns the `id` of the
    order. Errors: 400 for a kwami that is not the user's or an invalid address,
    409 when the PC is not online.
    """
    try:
        return {"id": await pc_service.show_site(user.id, body.kwami_id, body.address)}
    except pc_service.PcOfflineError as exc:
        raise HTTPException(status_code=409, detail="The PC is not there") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sites")
async def read_sites(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    """List the sites the user has opened on the PC.

    Requires a user JWT. Returns `sites` with host, url, state and the time last checked.
    """
    return {"sites": await pc_service.list_sites(user.id)}


@router.delete("/sites/{host}", status_code=204)
async def forget_site(host: str, user: Annotated[AuthUser, Depends(require_auth)]) -> None:
    """Take a site off the user's list.

    Requires a user JWT. Returns 204 with no body, also when the host was not listed. The session itself
    stays in Chrome.
    """
    await pc_service.forget_site(user.id, host)


@router.get("/orders/{order_id}")
async def read_order(
    order_id: UUID, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, Any]:
    """Read the state of a PC order, a login or a site.

    Requires a user JWT. Returns its `status` and `result`. Returns 404 when the order does not exist
    or is not the user's.
    """
    state = await pc_service.order_state(user.id, str(order_id))
    if state is None:
        raise HTTPException(status_code=404, detail="No such order")
    return state
