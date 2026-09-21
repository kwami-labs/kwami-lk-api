"""Per-caller rate limits.

There were none. `src/core/errors.py` has mapped 429 to `rate_limited` since the
error layer was written, so the envelope was waiting for an implementation --
meanwhile `/token` (which writes a `livekit_sessions` row per call),
`/credits/purchase` and `/channels/phone/purchase` (which spends real money at
Twilio) were all unbounded, and so were the unauthenticated webhooks.

Identity, in order of preference: the authenticated user, then the agent or
admin shared key, then the client IP. Keying on the user rather than the IP is
what stops one office NAT from sharing a single budget -- and what stops one
account from buying itself more headroom by changing address.

Storage is in-process, so limits are per worker. With several machines the
effective ceiling is the limit times the machine count, which is the right
shape for abuse control but not for quota. `RATE_LIMIT_STORAGE_URI` accepts a
`redis://` URL to make it exact once there is a Redis to point at.
"""

from __future__ import annotations

import logging

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.core.config import settings
from src.core.errors import error_body

logger = logging.getLogger("kwami-api.ratelimit")

# Chosen to be generous for a real client and cheap for an attacker to hit.
# A human never issues 30 tokens a minute; a script does it in a second.
TOKEN_LIMIT = "30/minute"  # noqa: S105 - a rate, not a credential
PURCHASE_LIMIT = "10/minute"
PROVISIONING_LIMIT = "5/minute"
WEBHOOK_LIMIT = "300/minute"
DEFAULT_LIMIT = "300/minute"


def rate_limit_key(request: Request) -> str:
    """Who this request is billed to.

    `request.state.user` is set by the auth dependency for authenticated routes.
    Falling back to the peer address covers the webhooks and the public catalogs.
    """
    user = getattr(request.state, "user", None)
    user_id = getattr(user, "id", None)
    if user_id:
        return f"user:{user_id}"

    for header in ("X-Kwami-API-Key", "X-Admin-API-Key"):
        key = request.headers.get(header)
        if key:
            # The key itself never reaches the store or the logs.
            return f"key:{hash(key)}"

    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[DEFAULT_LIMIT],
    storage_uri=settings.rate_limit_storage_uri,
    enabled=settings.rate_limit_enabled,
    # `headers_enabled=True` would add X-RateLimit-* to successful responses, but
    # slowapi injects them through a `response: Response` parameter it requires on
    # every decorated handler -- signature churn on each route for a header most
    # clients ignore. The budget is advertised where it matters instead: on the
    # 429 itself, via `rate_limit_exceeded_handler`.
    headers_enabled=False,
)


async def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a throttled request in the same envelope as every other error."""
    detail = getattr(exc, "detail", "Rate limit exceeded")
    logger.warning(
        "rate limit exceeded",
        extra={"path": request.url.path, "limit": str(detail)},
    )
    response = JSONResponse(
        status_code=429,
        content=error_body("rate_limited", "Too many requests. Please slow down."),
    )
    if isinstance(exc, RateLimitExceeded):
        # Tell a well-behaved client when to come back rather than making it guess.
        response.headers["Retry-After"] = str(getattr(exc, "retry_after", None) or 60)
        response.headers["X-RateLimit-Limit"] = str(detail)
    return response
