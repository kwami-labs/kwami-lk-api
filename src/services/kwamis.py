"""Ownership of a kwami: one query, one error, one place to change it.

There were four separate implementations of "does this user own this kwami?" --
``channels.get_owned_kwami``, ``wallet_service._resolve_owned_kwami``, an inline
query in ``credits.resolve_ledger_user_id`` and another in ``routes/internal.py``
-- with three different error shapes between them. The flagship endpoint that
needed it most, ``POST /token``, had none at all: it accepted any ``kwami_id``
from the request body and dispatched an agent with it.
"""

from __future__ import annotations

from typing import Any

from src.core.errors import KwamiNotFoundError
from src.services.credits import get_supabase_admin

# Enough for ownership decisions and for the callers that need the config blob.
KWAMI_COLUMNS = "id, user_id, name, config, created_at, updated_at"


def resolve_owned_kwami(
    user_id: str, kwami_id: str, *, columns: str = KWAMI_COLUMNS
) -> dict[str, Any]:
    """Return the kwami row, or raise ``KwamiNotFoundError``.

    Filters on ``user_id`` as well as ``id``, so a kwami belonging to someone else
    is indistinguishable from one that does not exist -- no existence oracle.
    """
    if not user_id or not kwami_id:
        raise KwamiNotFoundError()

    sb = get_supabase_admin()
    result = (
        sb.table("user_kwamis")
        .select(columns)
        .eq("id", kwami_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        raise KwamiNotFoundError()
    return rows[0]


def resolve_kwami(kwami_id: str, *, columns: str = KWAMI_COLUMNS) -> dict[str, Any]:
    """Return a kwami without a user filter.

    Only for key-authenticated backend routes (``/internal/*``) where there is no
    end user to scope by. Never reachable from a user-authenticated request.
    """
    if not kwami_id:
        raise KwamiNotFoundError()

    sb = get_supabase_admin()
    result = sb.table("user_kwamis").select(columns).eq("id", kwami_id).limit(1).execute()
    rows = getattr(result, "data", None) or []
    if not rows:
        raise KwamiNotFoundError()
    return rows[0]
