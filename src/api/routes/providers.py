"""`/providers`: the model accounts a user connects, for the brain to run on."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.api.ratelimit import PROVISIONING_LIMIT, limiter
from src.core.security import AuthUser
from src.services import provider_keys_service
from src.services.provider_keys_service import (
    InvalidKeyError,
    ProviderKeysNotConfiguredError,
    ProviderUnavailableError,
    UnknownProviderError,
)

router = APIRouter()

_PAGE = (
    "<!doctype html><html lang=es><meta charset=utf-8><title>Kwami</title>"
    "<body style='font-family:system-ui;text-align:center;padding:4rem'>"
    "<h1>{heading}</h1><p>{text}</p></body></html>"
)


class KeyRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=512)


@router.get("")
async def connections(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    return await provider_keys_service.list_connections(user.id)


@router.put("/{provider}/key")
@limiter.limit(PROVISIONING_LIMIT)
async def connect_key(
    request: Request,
    provider: str,
    body: KeyRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    try:
        return await provider_keys_service.connect_key(user.id, provider, body.key)
    except ProviderKeysNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Keys cannot be stored here") from exc
    except UnknownProviderError as exc:
        raise HTTPException(status_code=404, detail="Unknown provider") from exc
    except InvalidKeyError as exc:
        raise HTTPException(status_code=400, detail="The provider refused this key") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=502, detail="The provider did not answer") from exc


@router.delete("/{provider}")
async def disconnect(
    provider: str, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, bool]:
    if not await provider_keys_service.disconnect(user.id, provider):
        raise HTTPException(status_code=404, detail="Not connected")
    return {"ok": True}


@router.post("/openrouter/connect")
async def openrouter_connect(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, str]:
    try:
        return {"url": provider_keys_service.openrouter_authorization_url(user.id)}
    except ProviderKeysNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Keys cannot be stored here") from exc


@router.get("/openrouter/callback", response_class=HTMLResponse)
async def openrouter_callback(
    state: str = Query(default=""), code: str = Query(default="")
) -> HTMLResponse:
    """Where OpenRouter sends the browser. Unauthenticated: `state` says who it is for."""
    try:
        if not code:
            raise InvalidKeyError("Consent not given")
        await provider_keys_service.complete_openrouter(state, code)
    except (InvalidKeyError, ProviderUnavailableError, ProviderKeysNotConfiguredError):
        return HTMLResponse(
            _PAGE.format(
                heading="No se ha conectado", text="Vuelva a la app e inténtelo de nuevo."
            ),
            status_code=400,
        )
    return HTMLResponse(
        _PAGE.format(heading="OpenRouter conectado", text="Ya puede cerrar esta ventana.")
    )
