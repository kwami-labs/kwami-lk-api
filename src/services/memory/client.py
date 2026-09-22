"""The one Zep client this process uses.

`AsyncZep` owns an httpx.AsyncClient. A fresh one per request -- which is what
all 29 memory routes used to do -- is a fresh connection pool per request, none
of them ever closed, so sockets accumulated for as long as the worker lived.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from zep_cloud.client import AsyncZep

from src.core.config import settings

logger = logging.getLogger("kwami-api.memory")

# One client for the process. `AsyncZep` owns an httpx.AsyncClient, and this used
# to build a fresh one on every request across all 29 memory routes -- a new
# connection pool per request, none of them ever closed, so sockets accumulated
# for as long as the worker lived. Built lazily rather than in the lifespan so a
# deployment without ZEP_API_KEY still boots and answers 503 per route.
_zep_client: AsyncZep | None = None


async def get_zep_client() -> AsyncZep:
    global _zep_client
    if not settings.zep_api_key:
        raise HTTPException(
            status_code=503, detail="Memory service not configured (ZEP_API_KEY missing)"
        )
    if _zep_client is None:
        _zep_client = AsyncZep(api_key=settings.zep_api_key)
    return _zep_client


async def close_zep_client() -> None:
    """Release the shared client's connection pool at shutdown."""
    global _zep_client
    client = _zep_client
    _zep_client = None
    if client is None:
        return
    # zep-cloud has changed where it keeps the transport between versions, so
    # close whatever is there rather than reaching for one fixed attribute.
    for holder in (client, getattr(client, "_client_wrapper", None)):
        httpx_client = getattr(holder, "httpx_client", None) or getattr(holder, "_client", None)
        aclose = getattr(httpx_client, "aclose", None)
        if aclose is not None:
            await aclose()
            return
