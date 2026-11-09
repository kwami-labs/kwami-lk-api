"""`/providers`: the model accounts a user connects, for the brain to run on."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.api.ratelimit import PROVISIONING_LIMIT, limiter
from src.core.security import AuthUser
from src.services import oauth_return, provider_keys_service
from src.services.oauth_return import UnknownReturnError
from src.services.provider_keys_service import (
    InvalidKeyError,
    ProviderKeysNotConfiguredError,
    ProviderUnavailableError,
    UnknownProviderError,
    WrongUserError,
)

router = APIRouter()

_PAGE = (
    "<!doctype html><html lang=es><meta charset=utf-8><title>Kwami</title>"
    "<body style='font-family:system-ui;text-align:center;padding:4rem'>"
    "<h1>{heading}</h1><p>{text}</p></body></html>"
)


class KeyRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=512)


class ConnectRequest(BaseModel):
    desktop: bool = False


class CompleteRequest(BaseModel):
    state: str = Field(..., min_length=1, max_length=4096)
    code: str = Field(..., min_length=1, max_length=4096)


@router.get("")
async def connections(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    """List the model providers the user has connected.

    Requires a user JWT. Shows each provider with a hint of its key and when it was updated, never the
    key itself. Works when key storage is not configured: nothing is connected then.
    """
    return await provider_keys_service.list_connections(user.id)


@router.put("/{provider}/key")
@limiter.limit(PROVISIONING_LIMIT)
async def connect_key(
    request: Request,
    provider: str,
    body: KeyRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    """Store the user's API key for a provider, after the provider accepts it.

    Requires a user JWT. Rate limited. Errors: 400 when the provider refuses the key, 404 for an unknown
    provider, 502 when the provider does not answer, 503 when keys cannot be stored.
    """
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


@router.post("/{provider}/check")
@limiter.limit(PROVISIONING_LIMIT)
async def check_key(
    request: Request,
    provider: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, bool]:
    """Check that the provider still accepts the key connected for it.

    Requires a user JWT. Rate limited. Returns `{"ok": bool}`. Errors: 404 when the provider is not
    connected, 502 when the provider does not answer, 503 when keys cannot be stored.
    """
    try:
        return {"ok": await provider_keys_service.check_key(user.id, provider)}
    except ProviderKeysNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Keys cannot be stored here") from exc
    except UnknownProviderError as exc:
        raise HTTPException(status_code=404, detail="Not connected") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=502, detail="The provider did not answer") from exc


@router.delete("/{provider}")
async def disconnect(
    provider: str, user: Annotated[AuthUser, Depends(require_auth)]
) -> dict[str, bool]:
    """Forget the user's key for a provider.

    Requires a user JWT. Revoking the key is up to the user on the provider's site.
    Returns `{"ok": true}`. Returns 404 when the provider is not connected.
    """
    if not await provider_keys_service.disconnect(user.id, provider):
        raise HTTPException(status_code=404, detail="Not connected")
    return {"ok": True}


@router.post("/openrouter/connect")
async def openrouter_connect(
    request: Request,
    user: Annotated[AuthUser, Depends(require_auth)],
    body: ConnectRequest = ConnectRequest(),  # noqa: B008
) -> dict[str, str]:
    """Start the OpenRouter sign-in and return the URL to send the browser to.

    Requires a user JWT. The return target comes from the `Origin` header, or the desktop app when
    `desktop` is true. Errors: 400 for an unknown return target, 503 when keys
    cannot be stored.
    """
    try:
        target = oauth_return.return_target(request.headers.get("origin"), desktop=body.desktop)
        return {"url": provider_keys_service.openrouter_authorization_url(user.id, target)}
    except UnknownReturnError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderKeysNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Keys cannot be stored here") from exc


@router.get("/openrouter/callback", response_class=HTMLResponse)
async def openrouter_callback(
    state: str = Query(default=""), code: str = Query(default="")
) -> Response:
    """Where OpenRouter sends the browser. It connects nothing: see `oauth_return`."""
    try:
        if not code:
            raise InvalidKeyError("Consent not given")
        target = provider_keys_service.openrouter_return_target(state)
    except (InvalidKeyError, ProviderKeysNotConfiguredError):
        return HTMLResponse(
            _PAGE.format(
                heading="No se ha conectado", text="Vuelva a la app e inténtelo de nuevo."
            ),
            status_code=400,
        )
    return RedirectResponse(oauth_return.return_url(target, "openrouter", state, code), 302)


@router.post("/openrouter/complete")
@limiter.limit(PROVISIONING_LIMIT)
async def openrouter_complete(
    request: Request,
    body: CompleteRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, bool]:
    """The app, signed in, finishes what the callback passed on."""
    try:
        await provider_keys_service.complete_openrouter(user.id, body.state, body.code)
    except WrongUserError as exc:
        raise HTTPException(status_code=403, detail="Another user started this") from exc
    except InvalidKeyError as exc:
        raise HTTPException(status_code=400, detail="OpenRouter refused this") from exc
    except ProviderKeysNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Keys cannot be stored here") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=502, detail="The provider did not answer") from exc
    return {"ok": True}
