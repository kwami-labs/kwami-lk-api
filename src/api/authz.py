"""Reusable authorization dependencies.

``kwami_id`` reaches the API three ways -- path parameter, query parameter, and
request-body field -- so there are three shapes over the one resolver in
``src.services.kwamis``. Prefer the dependencies: they run before the handler, so
an unowned kwami never reaches business logic.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Query

from src.api.deps import require_auth
from src.core.security import AuthUser
from src.services.kwamis import resolve_owned_kwami


async def kwami_from_path(
    kwami_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    """For routes shaped `/.../kwamis/{kwami_id}`."""
    return resolve_owned_kwami(user.id, kwami_id)


async def kwami_from_query(
    user: Annotated[AuthUser, Depends(require_auth)],
    kwami_id: Annotated[str, Query(alias="kwamiId")],
) -> dict[str, Any]:
    """For routes taking `?kwamiId=` (contacts, email, calendar)."""
    return resolve_owned_kwami(user.id, kwami_id)


def require_kwami_owned(user_id: str, kwami_id: str) -> dict[str, Any]:
    """For `kwami_id` carried in a request body, where a dependency cannot read it."""
    return resolve_owned_kwami(user_id, kwami_id)


OwnedKwamiPath = Annotated[dict[str, Any], Depends(kwami_from_path)]
OwnedKwamiQuery = Annotated[dict[str, Any], Depends(kwami_from_query)]
