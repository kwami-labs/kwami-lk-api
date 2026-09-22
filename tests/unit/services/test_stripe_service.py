"""`src.services.stripe_service` — checkout creation, dispatch, and refunds.

The webhook's signature and replay behaviour are covered end-to-end in
tests/unit/api/test_stripe_webhook.py. This file covers the service directly:
checkout-session construction, the event-type routing table, and the refund
clawback — the path that writes the `'refund'` ledger type nothing used to write.
"""

from __future__ import annotations

import pytest

from src.services import stripe_service
from src.services.credits import CREDIT_PACKS, MICRO_CREDITS_PER_CREDIT
from src.services.stripe_service import (
    _dispatch_event,
    _handle_checkout_completed,
    _handle_refund,
    _init_stripe,
    create_checkout_session,
    handle_webhook_event,
)


class TestInitStripe:
    def test_it_sets_the_api_key(self, monkeypatch):
        monkeypatch.setattr(
            stripe_service.settings, "stripe_secret_key", "sk_test_x", raising=False
        )
        monkeypatch.setattr(stripe_service.stripe, "api_key", None, raising=False)
        _init_stripe()
        assert stripe_service.stripe.api_key == "sk_test_x"

    def test_an_unconfigured_key_is_a_runtime_error(self, monkeypatch):
        monkeypatch.setattr(stripe_service.settings, "stripe_secret_key", None, raising=False)
        with pytest.raises(RuntimeError, match="STRIPE_SECRET_KEY"):
            _init_stripe()


class TestCreateCheckoutSession:
    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        monkeypatch.setattr(
            stripe_service.settings, "stripe_secret_key", "sk_test_x", raising=False
        )

    @pytest.mark.anyio
    async def test_it_builds_the_session_from_the_pack(self, monkeypatch):
        captured: dict = {}

        async def create(**kwargs):
            captured.update(kwargs)
            return type("S", (), {"id": "cs_1", "url": "https://checkout.example/cs_1"})()

        # `create_async`, not `create`: the checkout call moved to Stripe's async
        # API so it stops blocking the event loop on an HTTPS round trip.
        monkeypatch.setattr(stripe_service.stripe.checkout.Session, "create_async", create)
        url = await create_checkout_session("u1", "pro", "https://ok", "https://no")

        assert url == "https://checkout.example/cs_1"
        pack = CREDIT_PACKS["pro"]
        line = captured["line_items"][0]
        assert line["price_data"]["unit_amount"] == pack["price_cents"]
        assert line["price_data"]["currency"] == "usd"
        assert pack["name"] in line["price_data"]["product_data"]["name"]
        assert captured["mode"] == "payment"
        assert captured["success_url"] == "https://ok"
        assert captured["cancel_url"] == "https://no"
        assert captured["client_reference_id"] == "u1"

    @pytest.mark.anyio
    async def test_the_metadata_carries_everything_the_webhook_needs(self, monkeypatch):
        """The webhook has only this metadata to decide who to credit and how much."""
        captured: dict = {}

        async def _create(**kw):
            captured.update(kw)
            return type("S", (), {"id": "cs_1", "url": "https://u"})()

        monkeypatch.setattr(stripe_service.stripe.checkout.Session, "create_async", _create)
        await create_checkout_session("u1", "starter", "https://ok", "https://no")
        assert captured["metadata"] == {
            "user_id": "u1",
            "pack_id": "starter",
            "credits": str(CREDIT_PACKS["starter"]["credits"]),
        }

    @pytest.mark.anyio
    async def test_an_unknown_pack_is_refused_before_stripe_is_called(self, monkeypatch):
        def explode(**kwargs):
            raise AssertionError("must not reach Stripe")

        monkeypatch.setattr(stripe_service.stripe.checkout.Session, "create_async", explode)
        with pytest.raises(ValueError, match="Invalid pack_id"):
            await create_checkout_session("u1", "no-such-pack", "https://ok", "https://no")

    @pytest.mark.anyio
    @pytest.mark.parametrize("pack_id", list(CREDIT_PACKS))
    async def test_every_shipped_pack_can_be_purchased(self, monkeypatch, pack_id):
        async def _create(**_kw):
            return type("S", (), {"id": "cs", "url": "https://u"})()

        monkeypatch.setattr(stripe_service.stripe.checkout.Session, "create_async", _create)
        assert await create_checkout_session("u1", pack_id, "https://ok", "https://no")


class TestDispatchEvent:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "event_type",
        ["checkout.session.completed", "checkout.session.async_payment_succeeded"],
    )
    async def test_both_settlement_events_reach_the_credit_handler(self, monkeypatch, event_type):
        """Two events describe one payment; both must credit, dedup keeps it once."""
        seen: list = []

        async def handler(obj):
            seen.append(obj)
            return {"status": "credited"}

        monkeypatch.setattr(stripe_service, "_handle_checkout_completed", handler)
        assert await _dispatch_event(event_type, {"id": "cs_1"}) == {"status": "credited"}
        assert seen == [{"id": "cs_1"}]

    @pytest.mark.anyio
    @pytest.mark.parametrize("event_type", ["charge.refunded", "refund.created"])
    async def test_both_refund_events_reach_the_refund_handler(self, monkeypatch, event_type):
        async def handler(obj):
            return {"status": "refunded"}

        monkeypatch.setattr(stripe_service, "_handle_refund", handler)
        assert await _dispatch_event(event_type, {"id": "ch_1"}) == {"status": "refunded"}

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "event_type",
        [
            "checkout.session.expired",
            "checkout.session.async_payment_failed",
            "payment_intent.payment_failed",
        ],
    )
    async def test_non_completion_events_are_noted(self, event_type, caplog):
        with caplog.at_level("INFO", logger="kwami-api.stripe"):
            assert await _dispatch_event(event_type, {}) == {
                "status": "noted",
                "event_type": event_type,
            }
        assert "did not complete" in caplog.text

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "event_type",
        ["charge.dispute.created", "charge.dispute.closed", "charge.dispute.funds_withdrawn"],
    )
    async def test_disputes_are_noted_loudly_rather_than_handled(self, event_type, caplog):
        """The clawback policy is a business decision, not a default."""
        with caplog.at_level("WARNING", logger="kwami-api.stripe"):
            assert await _dispatch_event(event_type, {}) == {
                "status": "noted",
                "event_type": event_type,
            }
        assert "dispute event received" in caplog.text

    @pytest.mark.anyio
    async def test_anything_else_is_ignored(self):
        assert await _dispatch_event("customer.created", {}) == {
            "status": "ignored",
            "event_type": "customer.created",
        }


class TestHandleCheckoutCompleted:
    @pytest.mark.anyio
    async def test_a_paid_session_credits_the_user(self, fake_supabase, tenant):
        result = await _handle_checkout_completed(
            {
                "id": "cs_1",
                "payment_status": "paid",
                "payment_intent": "pi_1",
                "amount_total": 500,
                "metadata": {"user_id": tenant.user_id, "pack_id": "starter", "credits": "5000"},
            }
        )
        assert result["status"] == "credited"
        assert result["credits_added"] == 5000
        assert result["user_id"] == tenant.user_id

    @pytest.mark.anyio
    async def test_the_payment_intent_is_recorded_for_a_later_refund(self, fake_supabase, tenant):
        """Refund events carry the payment intent, not the checkout session."""
        await _handle_checkout_completed(
            {
                "id": "cs_1",
                "payment_status": "paid",
                "payment_intent": "pi_1",
                "amount_total": 500,
                "metadata": {"user_id": tenant.user_id, "pack_id": "starter", "credits": "5000"},
            }
        )
        (txn,) = [
            r for r in fake_supabase.db.rows("credit_transactions") if r["type"] == "purchase"
        ]
        assert txn["metadata"]["stripe_payment_intent"] == "pi_1"
        assert txn["metadata"]["stripe_session_id"] == "cs_1"
        assert txn["metadata"]["amount_paid_cents"] == 500

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "metadata",
        [
            {},
            {"user_id": "u1"},
            {"pack_id": "starter"},
            {"user_id": "", "pack_id": "starter"},
        ],
        ids=["empty", "no pack", "no user", "blank user"],
    )
    async def test_missing_metadata_is_an_error_result(self, fake_supabase, metadata, caplog):
        with caplog.at_level("ERROR", logger="kwami-api.stripe"):
            result = await _handle_checkout_completed(
                {"id": "cs_1", "payment_status": "paid", "metadata": metadata}
            )
        assert result == {"status": "error", "reason": "missing metadata"}
        assert "missing metadata" in caplog.text

    @pytest.mark.anyio
    @pytest.mark.parametrize("status", ["unpaid", "no_payment_required", None])
    async def test_an_unpaid_session_is_skipped(self, fake_supabase, tenant, status, caplog):
        with caplog.at_level("WARNING", logger="kwami-api.stripe"):
            result = await _handle_checkout_completed(
                {
                    "id": "cs_1",
                    "payment_status": status,
                    "metadata": {
                        "user_id": tenant.user_id,
                        "pack_id": "starter",
                        "credits": "5000",
                    },
                }
            )
        assert result["status"] == "skipped"
        assert "not paid" in caplog.text

    @pytest.mark.anyio
    async def test_an_unknown_pack_id_still_credits_under_its_own_name(self, fake_supabase, tenant):
        """A pack renamed in config must not stop a paid purchase from landing."""
        result = await _handle_checkout_completed(
            {
                "id": "cs_1",
                "payment_status": "paid",
                "metadata": {
                    "user_id": tenant.user_id,
                    "pack_id": "legacy-pack",
                    "credits": "777",
                },
            }
        )
        assert result["status"] == "credited"
        assert result["credits_added"] == 777

    @pytest.mark.anyio
    async def test_credits_are_converted_to_micro_credits(self, fake_supabase, tenant):
        await _handle_checkout_completed(
            {
                "id": "cs_1",
                "payment_status": "paid",
                "metadata": {"user_id": tenant.user_id, "pack_id": "starter", "credits": "10"},
            }
        )
        (txn,) = [
            r for r in fake_supabase.db.rows("credit_transactions") if r["type"] == "purchase"
        ]
        assert txn["amount"] == 10 * MICRO_CREDITS_PER_CREDIT


class TestHandleRefund:
    async def _purchase(self, fake_supabase, tenant, *, micro=5_000_000, intent="pi_1"):
        fake_supabase.db.seed(
            "credit_transactions",
            {
                "user_id": tenant.user_id,
                "amount": micro,
                "type": "purchase",
                "idempotency_key": f"stripe:session:{intent}",
                "metadata": {"stripe_payment_intent": intent},
            },
        )

    @pytest.mark.anyio
    async def test_a_refund_claws_back_the_credits(self, fake_supabase, tenant):
        await self._purchase(fake_supabase, tenant, micro=1_000_000)
        _set_balance(fake_supabase, tenant.user_id, 1_000_000)

        result = await _handle_refund(
            {"id": "ch_1", "payment_intent": "pi_1", "amount_refunded": 500}
        )
        assert result["status"] == "refunded"
        assert result["clawed_back_micro"] == 1_000_000
        assert result["shortfall_micro"] == 0

    @pytest.mark.anyio
    async def test_the_clawback_is_clamped_at_the_current_balance(self, fake_supabase, tenant):
        """Spent credits leave a shortfall; the ledger must not go negative."""
        await self._purchase(fake_supabase, tenant, micro=1_000_000)
        _set_balance(fake_supabase, tenant.user_id, 400_000)

        result = await _handle_refund(
            {"id": "ch_1", "payment_intent": "pi_1", "amount_refunded": 500}
        )
        assert result["clawed_back_micro"] == 400_000
        assert result["shortfall_micro"] == 600_000

    @pytest.mark.anyio
    async def test_a_shortfall_is_recorded_on_the_transaction_and_logged(
        self, monkeypatch, fake_supabase, tenant, caplog
    ):
        """Asserted at the `deduct_credits` seam.

        The ledger row is written by the `deduct_credits` SQL function, not by
        Python, and FakeSupabase does not replay an RPC's side effects -- that is
        what tests/integration/db/ is for. What this layer owns is the argument
        it hands down, so that is what is checked.
        """
        await self._purchase(fake_supabase, tenant, micro=1_000_000)
        _set_balance(fake_supabase, tenant.user_id, 400_000)
        calls = _spy_deduct(monkeypatch)

        with caplog.at_level("WARNING", logger="kwami-api.stripe"):
            await _handle_refund({"id": "ch_1", "payment_intent": "pi_1", "amount_refunded": 500})

        assert "shortfall" in caplog.text
        (call,) = calls
        assert call["amount_micro"] == 400_000
        assert call["metadata"]["shortfall_micro"] == 600_000
        assert call["metadata"]["granted_micro"] == 1_000_000
        assert call["metadata"]["stripe_charge_id"] == "ch_1"
        assert call["metadata"]["stripe_payment_intent"] == "pi_1"
        assert call["metadata"]["original_transaction_id"] is not None

    @pytest.mark.anyio
    async def test_a_fully_spent_balance_writes_no_debit_but_still_reports(
        self, monkeypatch, fake_supabase, tenant
    ):
        """`if clawback > 0` -- a zero-amount deduct would be a pointless ledger row."""
        await self._purchase(fake_supabase, tenant, micro=1_000_000)
        _set_balance(fake_supabase, tenant.user_id, 0)
        calls = _spy_deduct(monkeypatch)

        result = await _handle_refund(
            {"id": "ch_1", "payment_intent": "pi_1", "amount_refunded": 500}
        )
        assert result["clawed_back_micro"] == 0
        assert result["shortfall_micro"] == 1_000_000
        assert calls == []

    @pytest.mark.anyio
    async def test_an_unmatched_refund_is_flagged_for_a_human(self, fake_supabase, caplog):
        with caplog.at_level("WARNING", logger="kwami-api.stripe"):
            result = await _handle_refund(
                {"id": "ch_9", "payment_intent": "pi_unknown", "amount_refunded": 500}
            )
        assert result == {"status": "unmatched", "charge_id": "ch_9"}
        assert "no matching credit transaction" in caplog.text

    @pytest.mark.anyio
    async def test_a_missing_amount_refunded_defaults_to_zero(
        self, monkeypatch, fake_supabase, tenant
    ):
        await self._purchase(fake_supabase, tenant, micro=1_000_000)
        _set_balance(fake_supabase, tenant.user_id, 1_000_000)
        calls = _spy_deduct(monkeypatch)
        result = await _handle_refund({"id": "ch_1", "payment_intent": "pi_1"})
        assert result["status"] == "refunded"
        assert calls[0]["metadata"]["amount_refunded_cents"] == 0


class TestHandleWebhookEvent:
    @pytest.mark.anyio
    async def test_an_unconfigured_webhook_secret_is_a_runtime_error(self, monkeypatch):
        monkeypatch.setattr(stripe_service.settings, "stripe_secret_key", "sk", raising=False)
        monkeypatch.setattr(stripe_service.settings, "stripe_webhook_secret", None, raising=False)
        with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
            await handle_webhook_event(b"{}", "sig")

    @pytest.mark.anyio
    async def test_a_handler_failure_is_recorded_then_re_raised(self, monkeypatch, fake_supabase):
        """Stripe must see the non-2xx and retry; the failure must also be on record."""
        monkeypatch.setattr(stripe_service, "_init_stripe", lambda: None)
        monkeypatch.setattr(
            stripe_service.settings, "stripe_webhook_secret", "whsec", raising=False
        )

        class FakeEvent:
            @staticmethod
            def to_dict():
                return {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {}}}

        monkeypatch.setattr(
            stripe_service.stripe.Webhook, "construct_event", lambda *a, **k: FakeEvent()
        )

        async def boom(event_type, obj):
            raise RuntimeError("handler exploded")

        monkeypatch.setattr(stripe_service, "_dispatch_event", boom)

        with pytest.raises(RuntimeError, match="handler exploded"):
            await handle_webhook_event(b"{}", "sig")

        (row,) = fake_supabase.db.rows("payment_events")
        assert row["status"] == "failed"
        assert "handler exploded" in row["error"]

    @pytest.mark.anyio
    async def test_an_ignored_event_is_completed_as_ignored(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(stripe_service, "_init_stripe", lambda: None)
        monkeypatch.setattr(
            stripe_service.settings, "stripe_webhook_secret", "whsec", raising=False
        )

        class FakeEvent:
            @staticmethod
            def to_dict():
                return {"id": "evt_2", "type": "customer.created", "data": {"object": {}}}

        monkeypatch.setattr(
            stripe_service.stripe.Webhook, "construct_event", lambda *a, **k: FakeEvent()
        )
        result = await handle_webhook_event(b"{}", "sig")
        assert result["status"] == "ignored"
        assert fake_supabase.db.rows("payment_events")[0]["status"] == "ignored"


def _spy_deduct(monkeypatch) -> list[dict]:
    """Capture the `deduct_credits` calls `_handle_refund` makes."""
    calls: list[dict] = []

    async def spy(*, user_id, amount_micro, description=None, metadata=None):
        calls.append(
            {
                "user_id": user_id,
                "amount_micro": amount_micro,
                "description": description,
                "metadata": metadata or {},
            }
        )
        return 0

    monkeypatch.setattr(stripe_service, "deduct_credits", spy)
    return calls


def _set_balance(fake_supabase, user_id: str, balance: int) -> None:
    for row in fake_supabase.db.rows("user_credits"):
        if str(row["user_id"]) == str(user_id):
            row["balance"] = balance
            return
    fake_supabase.db.seed(
        "user_credits",
        {"user_id": user_id, "balance": balance, "lifetime_purchased": 0, "lifetime_used": 0},
    )
