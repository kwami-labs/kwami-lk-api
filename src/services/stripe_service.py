"""Stripe integration service.

Handles Stripe Checkout session creation and webhook processing
for the credit purchase flow.
"""

import logging

import stripe

from src.core.config import settings
from src.services.credits import (
    CREDIT_PACKS,
    MICRO_CREDITS_PER_CREDIT,
    add_credits,
    deduct_credits,
    get_balance,
    get_supabase_admin,
)
from src.services.idempotency import claim_event, complete_event, ledger_key

logger = logging.getLogger("kwami-api.stripe")


def _init_stripe() -> None:
    """Initialize the Stripe SDK with the configured secret key.

    The async request path needs an async HTTP client as well as the key. Stripe
    ships `HTTPXClient` for exactly this; without it the `*_async` methods raise
    at call time rather than falling back to the blocking transport.
    """
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY must be set for payment processing")
    stripe.api_key = settings.stripe_secret_key
    if stripe.default_http_client is None:
        stripe.default_http_client = stripe.HTTPXClient()


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

    session = await stripe.checkout.Session.create_async(
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

    logger.info(
        "Created Stripe checkout session %s for user %s, pack=%s", session.id, user_id, pack_id
    )

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
        signed_event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.stripe_webhook_secret,
        )
    except stripe.SignatureVerificationError as e:
        # `stripe.error` is gone in stripe>=13; only a lazy module alias kept the
        # old path working, and it will not survive the next major.
        logger.warning("Stripe webhook signature verification failed: %s", e)
        raise ValueError("Invalid signature") from e

    # `to_dict()` at the boundary, once, and plain dicts from here down.
    #
    # stripe>=15 stopped making StripeObject a subclass of dict, so `.get()`,
    # iteration and `dict(event)` are all gone. The failure mode was not a clean
    # break either: `event.get("id")` raised AttributeError, but `dict(event)`
    # quietly returned `{}` -- an idempotency claim recorded with an empty
    # payload. Converting here keeps every handler below on the `dict` their
    # signatures already promise, and keeps this module off the SDK's object
    # model, which has now changed shape twice.
    event = signed_event.to_dict()

    event_id = event.get("id", "")
    event_type = event["type"]
    logger.info("Received Stripe webhook: %s (%s)", event_type, event_id)

    # Claim before processing. Stripe retries on any non-2xx and can redeliver an
    # event it already sent; without this, each retry credited the user again.
    if not await claim_event("stripe", event_id, event_type, payload=event):
        return {"status": "duplicate", "event_id": event_id, "event_type": event_type}

    try:
        result = await _dispatch_event(event_type, event["data"]["object"])
    except Exception as exc:
        await complete_event("stripe", event_id, status="failed", error=str(exc)[:500])
        raise

    await complete_event(
        "stripe",
        event_id,
        status="processed" if result.get("status") != "ignored" else "ignored",
        result=result,
    )
    return result


async def _dispatch_event(event_type: str, obj: dict) -> dict:
    """Route a verified Stripe event to its handler.

    Only `checkout.session.completed` was handled before. The gaps mattered:
    a session that settles asynchronously returned "skipped" at
    `payment_status != "paid"` and was never revisited, so the user paid and
    never received credits; and `'refund'` existed in the ledger enum with no
    writer anywhere, so a refunded payment kept its credits.
    """
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return await _handle_checkout_completed(obj)

    if event_type in ("charge.refunded", "refund.created"):
        return await _handle_refund(obj)

    if event_type in (
        "checkout.session.expired",
        "checkout.session.async_payment_failed",
        "payment_intent.payment_failed",
    ):
        logger.info("Stripe payment did not complete: %s", event_type)
        return {"status": "noted", "event_type": event_type}

    if event_type.startswith("charge.dispute."):
        # Disputed funds are held by Stripe; the credits are already spent or
        # spendable. Recorded loudly rather than handled silently, because the
        # clawback policy is a business decision, not a default.
        logger.warning("Stripe dispute event received: %s", event_type)
        return {"status": "noted", "event_type": event_type}

    return {"status": "ignored", "event_type": event_type}


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
            "Checkout session %s missing metadata: user_id=%s, pack_id=%s",
            stripe_session_id,
            user_id,
            pack_id,
        )
        return {"status": "error", "reason": "missing metadata"}

    if payment_status != "paid":
        logger.warning("Checkout session %s not paid: %s", stripe_session_id, payment_status)
        return {"status": "skipped", "reason": f"payment_status={payment_status}"}

    credits = int(credits_str)
    micro_credits = credits * MICRO_CREDITS_PER_CREDIT

    pack = CREDIT_PACKS.get(pack_id, {})
    pack_name = pack.get("name", pack_id)

    # Keyed on the checkout session, not the event: `completed` and
    # `async_payment_succeeded` are different events describing the same payment,
    # and event-id dedup alone would credit twice.
    new_balance = await add_credits(
        user_id=user_id,
        amount_micro=micro_credits,
        transaction_type="purchase",
        description=f"Purchased {pack_name} pack ({credits:,} credits)",
        metadata={
            "stripe_session_id": stripe_session_id,
            # Recorded so a later charge.refunded can find the credit it reverses:
            # refund events carry the payment intent, not the checkout session.
            "stripe_payment_intent": session.get("payment_intent"),
            "pack_id": pack_id,
            "credits": credits,
            "amount_paid_cents": session.get("amount_total"),
        },
        idempotency_key=ledger_key("stripe:session", stripe_session_id),
    )

    logger.info(
        "Credited %s credits to user %s (Stripe session: %s)",
        f"{credits:,}",
        user_id,
        stripe_session_id,
    )

    return {
        "status": "credited",
        "user_id": user_id,
        "credits_added": credits,
        "new_balance_micro": new_balance,
    }


async def _handle_refund(charge: dict) -> dict:
    """Claw back credits when a payment is refunded.

    `credit_transaction_type` has included `'refund'` since the first migration
    and nothing has ever written one: money could leave via Stripe while the
    credits it bought stayed spendable.

    The debit is clamped at the current balance rather than driving it negative.
    A user who already spent the credits leaves a shortfall, which is recorded on
    the transaction for follow-up instead of being silently absorbed.
    """
    charge_id = charge.get("id")
    payment_intent = charge.get("payment_intent")
    amount_refunded_cents = charge.get("amount_refunded") or 0

    sb = get_supabase_admin()
    lookup = (
        await sb.table("credit_transactions")
        .select("id, user_id, amount, metadata")
        .eq("type", "purchase")
        .eq("metadata->>stripe_payment_intent", str(payment_intent))
        .limit(1)
        .execute()
    )
    rows = getattr(lookup, "data", None) or []
    if not rows:
        # Nothing to reverse that we can attribute. Recorded rather than dropped:
        # a refund with no matching credit needs a human, not a silent 200.
        logger.warning(
            "Stripe refund %s has no matching credit transaction (payment_intent=%s)",
            charge_id,
            payment_intent,
        )
        return {"status": "unmatched", "charge_id": charge_id}

    original = rows[0]
    user_id = original["user_id"]
    granted_micro = int(original["amount"])

    balance = (await get_balance(user_id))["balance"]
    clawback = min(granted_micro, balance)
    shortfall = granted_micro - clawback

    if clawback > 0:
        await deduct_credits(
            user_id=user_id,
            amount_micro=clawback,
            description=f"Refund for charge {charge_id}",
            metadata={
                "stripe_charge_id": charge_id,
                "stripe_payment_intent": payment_intent,
                "amount_refunded_cents": amount_refunded_cents,
                "original_transaction_id": original["id"],
                "granted_micro": granted_micro,
                "shortfall_micro": shortfall,
            },
        )

    if shortfall:
        logger.warning(
            "Refund for charge %s left a shortfall of %d micro-credits for user %s "
            "(credits were already spent)",
            charge_id,
            shortfall,
            user_id,
        )

    return {
        "status": "refunded",
        "charge_id": charge_id,
        "user_id": user_id,
        "clawed_back_micro": clawback,
        "shortfall_micro": shortfall,
    }
