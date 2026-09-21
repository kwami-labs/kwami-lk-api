"""Core business logic for the Kwami Email Smart Hub.

Handles account activation, inbound email processing, inbox queries, and
message state mutations (read / star / archive).  All DB access goes through
the Supabase admin client (service-role key) so RLS is bypassed server-side.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from src.core.config import settings
from src.services.credits import get_supabase_admin
from src.services.email_classifier import classify
from src.services.idempotency import insert_or_existing

logger = logging.getLogger("kwami-api.email")

# How many unread messages `get_unread_counts` will scan.
MAX_UNREAD_SCAN = 2000

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,28}[a-z0-9]$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _single(result: Any) -> dict[str, Any] | None:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    return data


# ---------------------------------------------------------------------------
# Username validation & availability
# ---------------------------------------------------------------------------


def validate_username(username: str) -> str | None:
    """Return ``None`` if valid, else an error key suitable for i18n."""
    if not username:
        return "email.errors.usernameRequired"
    lower = username.lower()
    if len(lower) < 3:
        return "email.errors.usernameTooShort"
    if len(lower) > 30:
        return "email.errors.usernameTooLong"
    if not USERNAME_RE.match(lower):
        return "email.errors.usernameInvalid"
    return None


async def check_username_available(username: str) -> bool:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_accounts")
        .select("id")
        .eq("username", username.lower())
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    return len(rows) == 0


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------


async def get_account(user_id: str, kwami_id: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_accounts")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .limit(1)
        .execute()
    )
    return _single(result)


async def activate_account(
    user_id: str,
    kwami_id: str,
    username: str,
) -> dict[str, Any]:
    """Provision a ``username@kwami.io`` email for the given kwami.

    Raises ``ValueError`` for validation or uniqueness failures.
    """
    lower = username.lower()
    err = validate_username(lower)
    if err:
        raise ValueError(err)

    existing = await get_account(user_id, kwami_id)
    if existing:
        return existing

    if not await check_username_available(lower):
        raise ValueError("email.errors.usernameTaken")

    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_accounts")
        .insert(
            {
                "user_id": user_id,
                "kwami_id": kwami_id,
                "username": lower,
                "is_active": True,
            }
        )
        .execute()
    )
    row = _single(result)
    if not row:
        raise RuntimeError("Failed to create email account")
    logger.info("Email account activated: %s@%s", lower, settings.email_domain)
    return row


async def deactivate_account(user_id: str, kwami_id: str) -> bool:
    """Delete an email account and all its messages. Returns True if removed."""
    account = await get_account(user_id, kwami_id)
    if not account:
        return False

    sb = get_supabase_admin()
    # Messages are CASCADE-deleted via FK, but explicit delete is safer
    # in case RLS or triggers need to fire.
    await sb.table("kwami_email_messages").delete().eq("account_id", account["id"]).execute()
    await sb.table("kwami_email_accounts").delete().eq("id", account["id"]).execute()
    logger.info(
        "Email account deactivated: %s", account.get("email_address") or account["username"]
    )
    return True


# ---------------------------------------------------------------------------
# Inbound email processing
# ---------------------------------------------------------------------------


async def find_account_by_address(email_address: str) -> dict[str, Any] | None:
    """Look up an account by full email (username@kwami.io)."""
    local = email_address.split("@")[0].lower() if "@" in email_address else email_address.lower()
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_accounts")
        .select("*")
        .eq("username", local)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    return _single(result)


async def process_inbound_email(
    *,
    from_address: str,
    to_addresses: list[str],
    cc_addresses: list[str] | None = None,
    subject: str,
    body_text: str,
    body_html: str,
    headers: dict[str, Any] | None = None,
    sendgrid_message_id: str | None = None,
) -> dict[str, Any] | None:
    """Parse, classify, and store an inbound email.

    Returns the inserted row, or ``None`` if no matching account was found.
    """
    account: dict[str, Any] | None = None
    for addr in to_addresses:
        account = await find_account_by_address(addr)
        if account:
            break

    if not account:
        logger.warning("Inbound email to unknown address(es): %s", to_addresses)
        return None

    classification = classify(
        from_address=from_address,
        subject=subject,
        body_text=body_text,
    )

    # SendGrid retries a non-2xx, and `kwami_email_messages(sendgrid_message_id)`
    # is uniquely indexed, so a redelivery has to return the stored row rather
    # than raise -- see `insert_or_existing`.
    stored = await insert_or_existing(
        "kwami_email_messages",
        {
            "account_id": account["id"],
            "user_id": account["user_id"],
            "kwami_id": account["kwami_id"],
            "direction": "inbound",
            "from_address": from_address,
            "to_addresses": to_addresses,
            "cc_addresses": cc_addresses or [],
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "headers": headers or {},
            "sendgrid_message_id": sendgrid_message_id,
            "category": classification.category,
            "action_card_data": classification.action_card_data,
            "received_at": _now_iso(),
        },
        conflict_column="sendgrid_message_id",
    )
    row = stored
    if row:
        logger.info(
            "Inbound email stored id=%s category=%s",
            row["id"],
            classification.category,
        )
    return row


# ---------------------------------------------------------------------------
# Inbox queries
# ---------------------------------------------------------------------------


async def fetch_inbox(
    user_id: str,
    kwami_id: str,
    *,
    category: str | None = None,
    include_archived: bool = False,
    page: int = 1,
    page_size: int = 30,
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    q = (
        sb.table("kwami_email_messages")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .order("received_at", desc=True)
    )

    if not include_archived:
        q = q.eq("is_archived", False)
    if category and category != "all":
        q = q.eq("category", category)

    offset = (page - 1) * page_size
    q = q.range(offset, offset + page_size - 1)

    result = await q.execute()
    return list(getattr(result, "data", None) or [])


async def get_message(user_id: str, message_id: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_messages")
        .select("*")
        .eq("id", message_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return _single(result)


async def get_unread_counts(user_id: str, kwami_id: str) -> dict[str, int]:
    """Return a dict mapping each category to its unread count."""
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_messages")
        .select("category")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("is_read", False)
        .eq("is_archived", False)
        # Counting by reading every row is already the wrong shape for a large
        # mailbox; bounding it at least makes the number wrong in a visible,
        # fixed way rather than at PostgREST's silent 1000-row cap. A `count`
        # aggregate is the real fix.
        .limit(MAX_UNREAD_SCAN)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    counts: dict[str, int] = {}
    for row in rows:
        cat = row.get("category", "uncategorized")
        counts[cat] = counts.get(cat, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Message mutations
# ---------------------------------------------------------------------------


async def update_message(
    user_id: str,
    message_id: str,
    **fields: Any,
) -> dict[str, Any] | None:
    allowed = {"is_read", "is_starred", "is_archived"}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return await get_message(user_id, message_id)

    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_messages")
        .update(payload)
        .eq("id", message_id)
        .eq("user_id", user_id)
        .execute()
    )
    return _single(result)


# ---------------------------------------------------------------------------
# Outbound email helper
# ---------------------------------------------------------------------------


async def store_outbound_email(
    *,
    account: dict[str, Any],
    to_addresses: list[str],
    cc_addresses: list[str] | None = None,
    subject: str,
    body_text: str,
    body_html: str = "",
    sendgrid_message_id: str | None = None,
) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_email_messages")
        .insert(
            {
                "account_id": account["id"],
                "user_id": account["user_id"],
                "kwami_id": account["kwami_id"],
                "direction": "outbound",
                "from_address": account["email_address"],
                "to_addresses": to_addresses,
                "cc_addresses": cc_addresses or [],
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
                "sendgrid_message_id": sendgrid_message_id,
                "category": "personal",
                "is_read": True,
                "received_at": _now_iso(),
            }
        )
        .execute()
    )
    return _single(result)
