"""SendGrid helpers for outbound email and inbound webhook verification."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException

from src.core.config import settings

logger = logging.getLogger("kwami-api.sendgrid")

SENDGRID_SEND_URL = "https://api.sendgrid.com/v3/mail/send"


async def send_email(
    *,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    body_text: str = "",
    body_html: str = "",
    cc_addresses: list[str] | None = None,
    reply_to: str | None = None,
) -> str | None:
    """Send an email via the SendGrid v3 Mail Send API.

    Returns the SendGrid ``X-Message-Id`` on success or ``None`` on failure.
    """
    if not settings.sendgrid_api_key:
        raise RuntimeError("SENDGRID_API_KEY is not configured")

    personalizations: dict[str, Any] = {
        "to": [{"email": addr} for addr in to_addresses],
    }
    if cc_addresses:
        personalizations["cc"] = [{"email": addr} for addr in cc_addresses]

    payload: dict[str, Any] = {
        "personalizations": [personalizations],
        "from": {"email": from_address},
        "subject": subject,
        "content": [],
    }

    if body_text:
        payload["content"].append({"type": "text/plain", "value": body_text})
    if body_html:
        payload["content"].append({"type": "text/html", "value": body_html})
    if not payload["content"]:
        payload["content"].append({"type": "text/plain", "value": ""})

    if reply_to:
        payload["reply_to"] = {"email": reply_to}

    headers = {
        "Authorization": f"Bearer {settings.sendgrid_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(SENDGRID_SEND_URL, json=payload, headers=headers)

    if resp.status_code not in (200, 201, 202):
        logger.error(
            "SendGrid send failed status=%s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        return None

    message_id = resp.headers.get("X-Message-Id")
    logger.info("Email sent via SendGrid message_id=%s", message_id)
    return message_id


# SendGrid signs `timestamp + token`. Without a freshness window the timestamp is only
# an HMAC input, not a defence: the triple stays valid forever, so one captured delivery
# replays indefinitely. Five minutes is wide enough for ordinary clock skew.
INBOUND_SIGNATURE_MAX_AGE_SECONDS = 300


def verify_inbound_webhook(
    token: str,
    timestamp: str,
    signature: str,
) -> None:
    """Reject any inbound email SendGrid did not sign.

    This used to ``return True`` when ``SENDGRID_INBOUND_WEBHOOK_SECRET`` was unset,
    described as "development mode". The effect in production was that
    ``/webhooks/email/inbound`` accepted forged mail from anyone who found the URL,
    writing contacts, conversations and messages into a tenant's account -- the
    handler takes user_id and kwami_id from the looked-up account, not from the
    caller, so an unauthenticated POST lands inside real tenants.

    That is the identical defect ``src.services.twilio_service.validate_twilio_request``
    already documents having fixed; it was never carried across to SendGrid. This
    mirrors it: missing configuration fails closed, and the two inbound webhooks now
    behave the same way, which is what stops the next one from drifting.

    Raises ``HTTPException`` rather than returning a bool so a caller cannot forget to
    check the result.
    """
    secret = settings.sendgrid_inbound_webhook_secret
    if not secret:
        logger.error(
            "Inbound email received but SENDGRID_INBOUND_WEBHOOK_SECRET is not configured; "
            "refusing rather than accepting an unverified request"
        )
        raise HTTPException(
            status_code=503,
            detail="SendGrid webhook verification is not configured",
        )

    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail="Missing SendGrid signature")

    try:
        signed_at = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid SendGrid signature timestamp")

    # Symmetric: a timestamp far in the future is as suspect as a stale one, and
    # tolerating it would reopen the replay window from the other side.
    if abs(time.time() - signed_at) > INBOUND_SIGNATURE_MAX_AGE_SECONDS:
        logger.warning(
            "Rejecting inbound email signed at %s: outside the freshness window", signed_at
        )
        raise HTTPException(status_code=401, detail="SendGrid signature has expired")

    expected = hmac.new(
        secret.encode(),
        (timestamp + token).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid SendGrid signature")
