"""Stripe integration service.

Handles Stripe Checkout session creation and webhook processing
for the credit purchase flow.
"""

import logging

import stripe

from src.core.config import settings
from src.services.credits import CREDIT_PACKS, MICRO_CREDITS_PER_CREDIT, add_credits

logger = logging.getLogger("kwami-api.stripe")


def _init_stripe() -> None:
    """Initialize the Stripe SDK with the configured secret key."""
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY must be set for payment processing")
    stripe.api_key = settings.stripe_secret_key


async def create_checkout_session(
    user_id: str,
    pack_id: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create a Stripe Checkout Session for a credit pack purchase.

    Args:
        user_id: Supabase user ID.
        pack_id: One of 'starter', 'standard', 'pro'.
        success_url: URL to redirect to on successful payment.
        cancel_url: URL to redirect to on cancelled payment.

    Returns:
        The Stripe Checkout Session URL.

    Raises:
        ValueError: If the pack_id is invalid.
    """
    _init_stripe()

    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        raise ValueError(f"Invalid pack_id: {pack_id}. Must be one of {list(CREDIT_PACKS.keys())}")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack["price_cents"],
                    "product_data": {
                        "name": f"Kwami Energy - {pack['name']}",
                        "description": f"{pack['credits']:,} energy for your Kwami",
                    },
                },
                "quantity": 1,
            }
        ],
        metadata={
            "user_id": user_id,
            "pack_id": pack_id,
            "credits": str(pack["credits"]),
        },
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=user_id,
    )

    logger.info(f"Created Stripe checkout session {session.id} for user {user_id}, pack={pack_id}")

    return session.url


async def handle_webhook_event(payload: bytes, sig_header: str) -> dict:
    """Verify and process a Stripe webhook event.

    Args:
        payload: Raw request body bytes.
        sig_header: Stripe-Signature header value.

    Returns:
        Dict with processing result.

    Raises:
        ValueError: If signature verification fails.
    """
    _init_stripe()

    if not settings.stripe_webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET must be set")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.stripe_webhook_secret,
        )
    except stripe.error.SignatureVerificationError as e:
        logger.warning(f"Stripe webhook signature verification failed: {e}")
        raise ValueError("Invalid signature") from e

    logger.info(f"Received Stripe webhook: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        return await _handle_checkout_completed(session)

    return {"status": "ignored", "event_type": event["type"]}


async def _handle_checkout_completed(session: dict) -> dict:
    """Process a completed checkout session - add credits to user.

    Args:
        session: Stripe checkout session object.

    Returns:
        Dict with processing result.
    """
    metadata = session.get("metadata", {})
    user_id = metadata.get("user_id")
    pack_id = metadata.get("pack_id")
    credits_str = metadata.get("credits", "0")
    stripe_session_id = session.get("id")
    payment_status = session.get("payment_status")

    if not user_id or not pack_id:
        logger.error(
            f"Checkout session {stripe_session_id} missing metadata: "
            f"user_id={user_id}, pack_id={pack_id}"
        )
        return {"status": "error", "reason": "missing metadata"}

    if payment_status != "paid":
        logger.warning(f"Checkout session {stripe_session_id} not paid: {payment_status}")
        return {"status": "skipped", "reason": f"payment_status={payment_status}"}

    credits = int(credits_str)
    micro_credits = credits * MICRO_CREDITS_PER_CREDIT

    pack = CREDIT_PACKS.get(pack_id, {})
    pack_name = pack.get("name", pack_id)

    new_balance = await add_credits(
        user_id=user_id,
        amount_micro=micro_credits,
        transaction_type="purchase",
        description=f"Purchased {pack_name} pack ({credits:,} credits)",
        metadata={
            "stripe_session_id": stripe_session_id,
            "pack_id": pack_id,
            "credits": credits,
            "amount_paid_cents": session.get("amount_total"),
        },
    )

    logger.info(
        f"Credited {credits:,} credits to user {user_id} (Stripe session: {stripe_session_id})"
    )

    return {
        "status": "credited",
        "user_id": user_id,
        "credits_added": credits,
        "new_balance_micro": new_balance,
    }
