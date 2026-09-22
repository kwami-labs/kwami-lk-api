"""API dependencies."""

import hmac
import logging
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core.config import settings
from src.core.security import (
    AdminPrincipal,
    AuthUser,
    is_admin_user,
    is_valid_admin_api_key,
    verify_token,
)

logger = logging.getLogger("kwami-api.deps")

# Optional auth - auto_error=False allows unauthenticated requests
security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security_scheme)],
) -> AuthUser | None:
    """
    Decode and validate Supabase JWT via JWKS, return user info.

    Returns None if:
    - No credentials provided
    - Auth is not configured

    Raises HTTPException 401 if:
    - Token is invalid or expired
    """
    # If no credentials provided, return None (anonymous)
    if not credentials:
        return None

    # If auth is not configured, allow anonymous access
    if not settings.auth_enabled:
        logger.debug("Auth not configured, allowing anonymous access")
        return None

    try:
        payload = await verify_token(credentials.credentials)
        user = AuthUser(payload)
        # The rate limiter reads this to bill the account rather than the IP, so
        # one office NAT is not one budget and one account cannot buy headroom
        # by changing address. See src/api/ratelimit.py.
        request.state.user = user
        logger.debug("Authenticated user: %s", user)
        return user

    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.InvalidAudienceError:
        logger.warning("Invalid token audience")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token audience",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def require_auth(user: Annotated[AuthUser | None, Depends(get_current_user)]) -> AuthUser:
    """
    Dependency that requires authentication.

    Use this for endpoints that must have a valid authenticated user.
    Raises 401 if user is not authenticated.
    """
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_admin(
    user: Annotated[AuthUser | None, Depends(get_current_user)],
    x_admin_api_key: Annotated[str | None, Header(alias="X-Admin-API-Key")] = None,
) -> AdminPrincipal:
    """Require an admin identity via API key or authenticated allowlist."""
    if is_valid_admin_api_key(x_admin_api_key):
        return AdminPrincipal(auth_method="api_key")

    if is_admin_user(user):
        return AdminPrincipal(
            auth_method="user",
            user_id=user.id if user else None,
            email=user.email if user else None,
        )

    detail = "Admin access required"
    if settings.admin_api_key or settings.admin_emails:
        detail = "Valid admin credentials required"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


async def require_internal_api_key(
    x_kwami_api_key: Annotated[str | None, Header(alias="X-Kwami-API-Key")] = None,
) -> None:
    """Require the shared agent/API key for internal backend-to-backend routes."""
    if not settings.kwami_api_key or not x_kwami_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid internal API key required",
        )
    # Constant-time: `!=` on a shared secret leaks length and prefix through timing.
    if not hmac.compare_digest(x_kwami_api_key, settings.kwami_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid internal API key required",
        )
