"""Internal backend routes for agent bootstrap and service-to-service calls."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import require_internal_api_key
from src.services.channels import build_agent_bootstrap_payload, find_channel_by_address

router = APIRouter()


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
            sb.table("user_kwamis")
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
    channel = find_channel_by_address(address)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"channel": channel}
