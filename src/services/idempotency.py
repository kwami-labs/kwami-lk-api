"""Claim-before-process for provider webhooks, and ledger idempotency keys.

Stripe retries any non-2xx and can redeliver an event it has already sent. The
webhook handler verified the signature and then credited unconditionally, with no
event id stored and no unique constraint on the checkout session -- so every
retry granted the credits again.

The claim is an INSERT that either wins or hits ``UNIQUE (provider, event_id)``.
That matters: a read-then-write check has a window in which two concurrent
deliveries both see "not processed" and both proceed.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

from src.services.credits import get_supabase_admin

logger = logging.getLogger("kwami-api.idempotency")


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text or "already exists" in text


def claim_event(
    provider: str,
    event_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Record this delivery. Returns False if it was already recorded.

    A False return means "another delivery of this same event got here first" --
    the caller must not process it again.
    """
    if not event_id:
        # No id to deduplicate on. Processing is the lesser evil (dropping a real
        # payment is worse than a possible double), but it must be visible.
        logger.warning("Webhook from %s carried no event id; cannot deduplicate", provider)
        return True

    sb = get_supabase_admin()
    try:
        sb.table("payment_events").insert(
            {
                "provider": provider,
                "event_id": event_id,
                "event_type": event_type,
                "status": "received",
                "payload": payload or {},
            }
        ).execute()
        return True
    except Exception as exc:
        if _is_unique_violation(exc):
            logger.info("Ignoring duplicate %s webhook %s (%s)", provider, event_id, event_type)
            return False
        raise


def complete_event(
    provider: str,
    event_id: str,
    *,
    status: str = "processed",
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Record the outcome of a claimed event. Best effort: never mask the result."""
    if not event_id:
        return
    sb = get_supabase_admin()
    try:
        sb.table("payment_events").update(
            {
                "status": status,
                "result": result or {},
                "error": error,
                "processed_at": _now_iso(),
            }
        ).eq("provider", provider).eq("event_id", event_id).execute()
    except Exception:
        logger.exception("Could not record outcome for %s webhook %s", provider, event_id)


def ledger_key(namespace: str, *parts: str) -> str:
    """Build a ``credit_transactions.idempotency_key``.

    One namespace so the keys are greppable and cannot collide across flows:
    ``stripe:session:<id>``, ``stripe:refund:<charge>:<refund>``,
    ``wallet:intent:<uuid>``, ``usage:report:<key>``, ``bonus:welcome:<user>``.
    """
    return ":".join([namespace, *(str(p) for p in parts)])


def already_in_ledger(idempotency_key: str) -> bool:
    """Has a ledger row already been written under this key?

    The backstop for the case where two *different* events describe the same
    payment -- `checkout.session.completed` followed by
    `checkout.session.async_payment_succeeded`, for instance -- which event-id
    dedup alone cannot catch.
    """
    if not idempotency_key:
        return False
    sb = get_supabase_admin()
    result = (
        sb.table("credit_transactions")
        .select("id")
        .eq("idempotency_key", idempotency_key)
        .limit(1)
        .execute()
    )
    return bool(getattr(result, "data", None) or [])


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
