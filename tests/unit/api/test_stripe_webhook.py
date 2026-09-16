"""Stripe webhook: signature enforcement, replay safety, and event coverage.

The handler verified the signature and then credited unconditionally. No event id
was stored, there was no unique constraint on the checkout session, and there was
no pre-check -- so every Stripe retry (it retries any non-2xx, and redelivers
legitimately) granted the credits again.

Only `checkout.session.completed` was handled. A session that settles
asynchronously returned "skipped" and was never revisited, so the user paid and
got nothing; and `'refund'` existed in the ledger enum with no writer at all.
"""

import json
import time

import pytest
import stripe
from httpx import AsyncClient

from src.core.config import settings

WEBHOOK_PATH = "/credits/webhook"


def signed(event: dict, secret: str | None = None, timestamp: int | None = None):
    """A real Stripe signature, computed by Stripe's own signing code."""
    payload = json.dumps(event)
    ts = timestamp or int(time.time())
    secret = secret or settings.stripe_webhook_secret
    signature = stripe.WebhookSignature._compute_signature(f"{ts}.{payload}", secret)
    return payload, f"t={ts},v1={signature}"


def checkout_event(user_id: str, *, event_id="evt_1", session_id="cs_1", status="paid", credits=100):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "payment_status": status,
                "payment_intent": "pi_1",
                "amount_total": 1000,
                "metadata": {"user_id": user_id, "pack_id": "starter", "credits": str(credits)},
            }
        },
    }


async def post(client: AsyncClient, event: dict, **kwargs):
    payload, signature = signed(event, **kwargs)
    return await client.post(
        WEBHOOK_PATH,
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )


# --------------------------------------------------------------------------
# signature
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_missing_signature_is_rejected(client: AsyncClient, tenant):
    response = await client.post(WEBHOOK_PATH, json=checkout_event(tenant.user_id))
    assert response.status_code == 400


@pytest.mark.anyio
async def test_forged_signature_is_rejected(client: AsyncClient, tenant):
    payload = json.dumps(checkout_event(tenant.user_id))
    response = await client.post(
        WEBHOOK_PATH,
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_signature_from_the_wrong_secret_is_rejected(client: AsyncClient, tenant):
    response = await post(client, checkout_event(tenant.user_id), secret="whsec_not_ours")
    assert response.status_code == 400


@pytest.mark.anyio
async def test_stale_timestamp_is_rejected(client: AsyncClient, tenant):
    """Stripe's construct_event enforces a tolerance, which blocks replay."""
    old = int(time.time()) - 3600
    response = await post(client, checkout_event(tenant.user_id), timestamp=old)
    assert response.status_code == 400


# --------------------------------------------------------------------------
# crediting
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_paid_checkout_credits_the_user(client: AsyncClient, tenant, fake_supabase):
    response = await post(client, checkout_event(tenant.user_id, credits=100))
    assert response.status_code == 200

    balance = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]
    assert balance == 500_000 + 100_000


@pytest.mark.anyio
async def test_replaying_the_same_event_does_not_double_credit(
    client: AsyncClient, tenant, fake_supabase
):
    """The defect: Stripe retries, and every retry used to credit again."""
    event = checkout_event(tenant.user_id, credits=100)

    first = await post(client, event)
    second = await post(client, event)

    assert first.status_code == second.status_code == 200
    balance = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]
    assert balance == 500_000 + 100_000, "one payment, one credit"

    purchases = [r for r in fake_supabase.db.rows("credit_transactions") if r["type"] == "purchase"]
    assert len(purchases) == 1


@pytest.mark.anyio
async def test_a_different_event_for_the_same_session_credits_once(
    client: AsyncClient, tenant, fake_supabase
):
    """`completed` and `async_payment_succeeded` describe one payment.

    Event-id dedup alone cannot catch this; the ledger key on the checkout
    session is what makes it safe.
    """
    completed = checkout_event(tenant.user_id, event_id="evt_a", session_id="cs_same")
    async_paid = checkout_event(tenant.user_id, event_id="evt_b", session_id="cs_same")
    async_paid["type"] = "checkout.session.async_payment_succeeded"

    await post(client, completed)
    await post(client, async_paid)

    balance = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]
    assert balance == 500_000 + 100_000


@pytest.mark.anyio
async def test_an_unpaid_session_does_not_credit(client: AsyncClient, tenant, fake_supabase):
    response = await post(client, checkout_event(tenant.user_id, status="unpaid"))
    assert response.status_code == 200

    balance = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]
    assert balance == 500_000


@pytest.mark.anyio
async def test_an_async_payment_that_later_succeeds_is_credited(
    client: AsyncClient, tenant, fake_supabase
):
    """Previously this returned 'skipped' forever: paid user, no credits."""
    pending = checkout_event(tenant.user_id, event_id="evt_p", session_id="cs_async", status="unpaid")
    await post(client, pending)

    settled = checkout_event(tenant.user_id, event_id="evt_s", session_id="cs_async", status="paid")
    settled["type"] = "checkout.session.async_payment_succeeded"
    response = await post(client, settled)

    assert response.status_code == 200
    balance = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]
    assert balance == 500_000 + 100_000


@pytest.mark.anyio
async def test_unhandled_event_types_are_acknowledged(client: AsyncClient, tenant):
    event = checkout_event(tenant.user_id, event_id="evt_x")
    event["type"] = "customer.subscription.updated"
    response = await post(client, event)
    # Must be 2xx: a non-2xx makes Stripe retry an event we will never handle.
    assert response.status_code == 200
