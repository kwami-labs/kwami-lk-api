"""Internal backend routes for agent bootstrap and service-to-service calls."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import require_internal_api_key
from src.services.browser_sessions import (
    UnsupportedVendorError,
    delete_browser_context,
    get_browser_context,
    save_browser_context,
)
from src.services.channels import build_agent_bootstrap_payload, find_channel_by_address

router = APIRouter()


class BrowserContextIn(BaseModel):
    """The vendor-side handle for one owner's persisted browser profile."""

    vendor: str = Field(min_length=1, max_length=64)
    context_id: str = Field(min_length=1, max_length=255)


@router.get("/kwamis/{kwami_id}/runtime")
async def get_kwami_runtime_config(
    kwami_id: str,
    _: Annotated[None, Depends(require_internal_api_key)],
):
    # Internal routes are keyed, so we only need to load the kwami row and return
    # the config payload that mimics the browser's initial "config" message.
    try:
        from src.services.credits import get_supabase_admin

        sb = get_supabase_admin()
        result = (
            await sb.table("user_kwamis")
            .select("id, user_id, name, config")
            .eq("id", kwami_id)
            .limit(1)
            .execute()
        )
        data = getattr(result, "data", None) or []
        if not data:
            raise HTTPException(status_code=404, detail="Kwami not found")
        kwami = data[0]
        return build_agent_bootstrap_payload(kwami)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to load runtime config: {exc}"
        ) from exc


@router.get("/channels/by-address")
async def get_channel_by_address(
    address: str,
    _: Annotated[None, Depends(require_internal_api_key)],
):
    channel = await find_channel_by_address(address)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"channel": channel}


# ---------------------------------------------------------------------------
# Cloud-browser profiles
# ---------------------------------------------------------------------------
#
# The agent's navigation panel runs a cloud browser holding the user's cookies
# and logins. Browserbase addresses that persisted state by an opaque Context
# id handed back once at creation, with no lookup-by-name endpoint -- so if the
# id is not kept here, every session starts signed out of everything and leaves
# the previous context orphaned but still billed.
#
# These are internal routes: the agent calls them with the shared key. They
# deliberately expose no way to enumerate owners.


@router.get("/browser-contexts/{owner_key}")
async def get_browser_context_route(
    owner_key: str,
    _: Annotated[None, Depends(require_internal_api_key)],
    vendor: Annotated[str, Query(min_length=1, max_length=64)],
):
    """The stored browser profile handle for this owner, or 404."""
    try:
        context_id = await get_browser_context(owner_key, vendor)
    except UnsupportedVendorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to read browser context: {exc}"
        ) from exc

    if not context_id:
        raise HTTPException(status_code=404, detail="No saved browser context")
    return {"owner_key": owner_key, "vendor": vendor, "context_id": context_id}


@router.post("/browser-contexts/{owner_key}")
async def save_browser_context_route(
    owner_key: str,
    _: Annotated[None, Depends(require_internal_api_key)],
    payload: Annotated[BrowserContextIn, Body()],
):
    """Record the browser profile handle so the next session reuses it."""
    try:
        await save_browser_context(owner_key, payload.vendor, payload.context_id)
    except UnsupportedVendorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to save browser context: {exc}"
        ) from exc
    return {"owner_key": owner_key, "vendor": payload.vendor, "saved": True}


@router.delete("/browser-contexts/{owner_key}")
async def delete_browser_context_route(
    owner_key: str,
    _: Annotated[None, Depends(require_internal_api_key)],
    vendor: Annotated[str | None, Query(max_length=64)] = None,
):
    """Forget an owner's stored profile handle(s).

    Only drops our pointer. The vendor-side profile still exists and still
    holds the cookies, so a full "clear my browsing data" also has to delete it
    at the vendor.
    """
    try:
        removed = await delete_browser_context(owner_key, vendor)
    except UnsupportedVendorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete browser context: {exc}"
        ) from exc
    return {"owner_key": owner_key, "removed": removed}
