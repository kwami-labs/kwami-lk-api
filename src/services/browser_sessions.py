"""Where a user's persisted cloud-browser profile is remembered.

The agent runs a real cloud browser for the navigation panel, carrying the
user's cookies and logins so that "open my mail" opens *their* mail. The vendor
keeps that state under an opaque handle -- a Browserbase Context id, or a
Browser Use profile id -- and Browserbase in particular offers no way to look
one up again: the id returned at creation is the only way to ever reach that
context, and names are unique per project so re-creating one is rejected rather
than idempotent.

So the handle has to live somewhere that outlives an agent process. Losing it
is not a soft failure: the user is signed out of every site they had signed
into, and the previous context is orphaned -- still stored, still billed, and
now unreachable by anything.

`owner_key` is the agent's `kwami_id`, which is a user_kwamis id for web
sessions and a LiveKit participant identity for telephony ones. It is stored as
free text for that reason; see migrations/018_browser_contexts.sql.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.services.credits import get_supabase_admin

logger = logging.getLogger(__name__)

TABLE = "browser_contexts"

#: Kept in step with the CHECK constraint on browser_contexts.vendor. An
#: unknown vendor is rejected here rather than at the database, so the agent
#: gets a 400 it can log instead of a 500 it cannot.
SUPPORTED_VENDORS = frozenset({"browserbase", "browser_use"})

#: Vendor handles are opaque ids, not free text. Bounding them stops a buggy or
#: hostile caller from parking arbitrary payloads in the table.
MAX_CONTEXT_ID_LENGTH = 255
MAX_OWNER_KEY_LENGTH = 255


class UnsupportedVendorError(ValueError):
    """The caller named a cloud-browser vendor this table does not model."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _single(result: Any) -> dict[str, Any] | None:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    return data


def _validate(owner_key: str, vendor: str) -> tuple[str, str]:
    owner = (owner_key or "").strip()
    if not owner:
        raise ValueError("owner_key is required")
    if len(owner) > MAX_OWNER_KEY_LENGTH:
        raise ValueError("owner_key is too long")

    normalized_vendor = (vendor or "").strip().lower()
    if normalized_vendor not in SUPPORTED_VENDORS:
        raise UnsupportedVendorError(
            f"Unknown browser vendor '{vendor}'. "
            f"Expected one of: {', '.join(sorted(SUPPORTED_VENDORS))}"
        )
    return owner, normalized_vendor


def _as_user_id(owner_key: str) -> str | None:
    """Record the owning user when `owner_key` is one.

    Best-effort on purpose: it only drives the RLS policies that let a user
    audit and clear their own browsing state. A telephony identity is not a
    uuid and simply leaves the column null.
    """
    try:
        return str(UUID(owner_key))
    except (ValueError, AttributeError, TypeError):
        return None


async def get_browser_context(owner_key: str, vendor: str) -> str | None:
    """The stored vendor handle for this owner, or None."""
    owner, normalized_vendor = _validate(owner_key, vendor)

    sb = get_supabase_admin()
    result = (
        await sb.table(TABLE)
        .select("context_id")
        .eq("owner_key", owner)
        .eq("vendor", normalized_vendor)
        .limit(1)
        .execute()
    )
    row = _single(result)
    return (row or {}).get("context_id") or None


async def save_browser_context(owner_key: str, vendor: str, context_id: str) -> dict[str, Any]:
    """Record the vendor handle for this owner, replacing any previous one.

    Upserted on (owner_key, vendor) rather than inserted: two sessions starting
    at once would otherwise leave two rows, and whichever was read next would
    decide which of the user's two half-signed-in profiles they got.
    """
    owner, normalized_vendor = _validate(owner_key, vendor)

    handle = (context_id or "").strip()
    if not handle:
        raise ValueError("context_id is required")
    if len(handle) > MAX_CONTEXT_ID_LENGTH:
        raise ValueError("context_id is too long")

    # Timestamps are sent explicitly rather than left to a DEFAULT: an upsert
    # that resolves to an UPDATE does not re-run the column default, so
    # updated_at would stay frozen at the row's creation time.
    now_iso = _now_iso()
    payload: dict[str, Any] = {
        "owner_key": owner,
        "vendor": normalized_vendor,
        "context_id": handle,
        "updated_at": now_iso,
        "last_used_at": now_iso,
    }
    user_id = _as_user_id(owner)
    if user_id:
        payload["user_id"] = user_id

    sb = get_supabase_admin()
    result = await sb.table(TABLE).upsert(payload, on_conflict="owner_key,vendor").execute()
    row = _single(result) or payload
    logger.info("Saved %s browser context for owner %s...", normalized_vendor, owner[:8])
    return row


async def delete_browser_context(owner_key: str, vendor: str | None = None) -> int:
    """Forget an owner's stored profile handle(s). Returns rows removed.

    Used when a user asks to clear their browsing session. It only drops our
    pointer -- the vendor-side context has to be deleted through the vendor,
    which the caller is responsible for.
    """
    owner = (owner_key or "").strip()
    if not owner:
        raise ValueError("owner_key is required")

    sb = get_supabase_admin()
    query = sb.table(TABLE).delete().eq("owner_key", owner)
    if vendor:
        _, normalized_vendor = _validate(owner, vendor)
        query = query.eq("vendor", normalized_vendor)

    result = await query.execute()
    removed = len(getattr(result, "data", None) or [])
    logger.info("Deleted %d browser context row(s) for owner %s...", removed, owner[:8])
    return removed
