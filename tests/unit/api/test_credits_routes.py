"""`/credits` — balance, packs, checkout, history and the reconciliation view.

The webhook and the agent usage-report endpoint have their own files
(test_stripe_webhook.py, test_usage_report_endpoint.py). This covers the
user-facing reads, the Stripe hand-off and its two failure modes, and the
response mapping — which is where a nullable ledger column silently becomes a
500 if the model and the row disagree.
"""

from __future__ import annotations

import pytest

from src.api.routes import credits as credits_route
from src.services.credits import CREDIT_PACKS, MICRO_CREDITS_PER_CREDIT

pytestmark = pytest.mark.anyio


class TestBalance:
    async def test_it_reports_the_balance_in_both_units(self, tenant_client, tenant):
        r = await tenant_client.get("/credits/balance")
        assert r.status_code == 200
        body = r.json()
        assert body["balance"] == 500_000
        assert body["balance_credits"] == 500_000 / MICRO_CREDITS_PER_CREDIT
        assert body["lifetime_purchased"] == 500_000
        assert body["lifetime_used"] == 0

    async def test_a_user_with_no_ledger_row_reads_as_zero(self, auth_client, fake_supabase):
        for row in fake_supabase.db.rows("user_credits"):
            row["balance"] = 0
        r = await auth_client.get("/credits/balance")
        assert r.status_code == 200
        assert r.json()["balance"] == 0

    async def test_it_requires_auth(self, client):
        assert (await client.get("/credits/balance")).status_code == 401


class TestPacks:
    async def test_every_configured_pack_is_offered(self, client):
        r = await client.get("/credits/packs")
        assert r.status_code == 200
        assert {p["id"] for p in r.json()["packs"]} == set(CREDIT_PACKS)

    async def test_the_price_is_rendered_for_display(self, client):
        packs = {p["id"]: p for p in (await client.get("/credits/packs")).json()["packs"]}
        assert packs["starter"]["price_display"] == "$5.00"
        assert packs["pro"]["price_display"] == "$100.00"

    async def test_exactly_one_pack_is_marked_popular(self, client):
        packs = (await client.get("/credits/packs")).json()["packs"]
        assert sum(1 for p in packs if p["popular"]) == 1

    async def test_it_needs_no_auth(self, client):
        assert (await client.get("/credits/packs")).status_code == 200


class TestPurchase:
    def _body(self, **overrides):
        return {
            "pack_id": "starter",
            "success_url": "https://app.kwami.io/ok",
            "cancel_url": "https://app.kwami.io/no",
            **overrides,
        }

    async def test_it_returns_the_checkout_url(self, monkeypatch, tenant_client, tenant):
        seen = {}

        async def create(**kwargs):
            seen.update(kwargs)
            return "https://checkout.stripe.com/c/pay/cs_1"

        monkeypatch.setattr(credits_route, "create_checkout_session", create)
        r = await tenant_client.post("/credits/purchase", json=self._body())
        assert r.status_code == 200
        assert r.json()["checkout_url"].startswith("https://checkout.stripe.com")
        assert seen["user_id"] == tenant.user_id, "the session is bound to the caller"
        assert seen["pack_id"] == "starter"

    async def test_an_unknown_pack_is_a_400(self, monkeypatch, tenant_client):
        async def create(**kwargs):
            raise ValueError("Invalid pack_id: nope")

        monkeypatch.setattr(credits_route, "create_checkout_session", create)
        r = await tenant_client.post("/credits/purchase", json=self._body(pack_id="nope"))
        assert r.status_code == 400
        assert "Invalid pack_id" in r.json()["detail"]

    async def test_unconfigured_stripe_is_a_503(self, monkeypatch, tenant_client, caplog):
        """A missing key is an operator problem, not a client problem."""

        async def create(**kwargs):
            raise RuntimeError("STRIPE_SECRET_KEY must be set for payment processing")

        monkeypatch.setattr(credits_route, "create_checkout_session", create)
        with caplog.at_level("ERROR", logger="kwami-api.credits"):
            r = await tenant_client.post("/credits/purchase", json=self._body())
        assert r.status_code == 503
        assert r.json()["detail"] == "Payment processing is not currently available"
        assert "STRIPE_SECRET_KEY" not in r.text, "the key name stays in the log"
        assert "Stripe not configured" in caplog.text

    async def test_it_requires_auth(self, client):
        assert (await client.post("/credits/purchase", json=self._body())).status_code == 401


class TestTransactions:
    def _seed(self, fake_supabase, tenant, n=2):
        for i in range(n):
            fake_supabase.db.seed(
                "credit_transactions",
                {
                    "user_id": tenant.user_id,
                    "type": "purchase",
                    "amount": 1000 + i,
                    "balance_after": 2000 + i,
                    "description": f"txn {i}",
                    "metadata": {"i": i},
                    "idempotency_key": f"k{i}",
                },
            )

    async def test_it_lists_the_users_transactions(self, tenant_client, tenant, fake_supabase):
        self._seed(fake_supabase, tenant)
        r = await tenant_client.get("/credits/transactions")
        assert r.status_code == 200
        assert r.json()["count"] == 2
        assert len(r.json()["transactions"]) == 2

    async def test_an_empty_history_is_an_empty_list(self, tenant_client):
        r = await tenant_client.get("/credits/transactions")
        assert r.json() == {"transactions": [], "count": 0}

    async def test_a_transaction_without_a_description_is_still_serialisable(
        self, tenant_client, tenant, fake_supabase
    ):
        fake_supabase.db.seed(
            "credit_transactions",
            {
                "user_id": tenant.user_id,
                "type": "bonus",
                "amount": 1,
                "balance_after": 1,
                "idempotency_key": "k",
            },
        )
        r = await tenant_client.get("/credits/transactions")
        assert r.status_code == 200
        assert r.json()["transactions"][0]["description"] is None

    async def test_it_paginates(self, tenant_client, tenant, fake_supabase):
        self._seed(fake_supabase, tenant, n=5)
        assert (await tenant_client.get("/credits/transactions", params={"limit": 2})).json()[
            "count"
        ] == 2

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    async def test_out_of_range_pagination_is_a_422(self, tenant_client, params):
        assert (await tenant_client.get("/credits/transactions", params=params)).status_code == 422

    async def test_it_is_scoped_to_the_caller(self, other_tenant_client, tenant, fake_supabase):
        self._seed(fake_supabase, tenant)
        assert (await other_tenant_client.get("/credits/transactions")).json()["count"] == 0

    async def test_it_requires_auth(self, client):
        assert (await client.get("/credits/transactions")).status_code == 401


class TestUsage:
    def _seed(self, fake_supabase, tenant, n=2, session_id="s1", **extra):
        for i in range(n):
            fake_supabase.db.seed(
                "credit_usage_logs",
                {
                    "user_id": tenant.user_id,
                    "session_id": session_id,
                    "model_type": "llm",
                    "model_id": "openai/gpt-4o",
                    "units_used": 100 + i,
                    "cost_usd": 0.01,
                    "credits_charged": 10,
                    **extra,
                },
            )

    async def test_it_lists_usage(self, tenant_client, tenant, fake_supabase):
        self._seed(fake_supabase, tenant)
        r = await tenant_client.get("/credits/usage")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    async def test_the_optional_ledger_columns_may_be_absent(
        self, tenant_client, tenant, fake_supabase
    ):
        """Rows written before the margin columns existed still have to serialise."""
        self._seed(fake_supabase, tenant, n=1)
        item = (await tenant_client.get("/credits/usage")).json()["logs"][0]
        assert item["provider_cost_usd"] is None
        assert item["billed_cost_usd"] is None
        assert item["settlement_status"] is None

    async def test_the_optional_columns_are_carried_when_present(
        self, tenant_client, tenant, fake_supabase
    ):
        self._seed(
            fake_supabase,
            tenant,
            n=1,
            provider_cost_usd=0.01,
            billed_cost_usd=0.02,
            margin_usd=0.01,
            requested_credits=20,
            settlement_status="charged",
            pricing_version="2026-03-20",
            pricing_source="catalog",
            usage_metadata={"k": "v"},
        )
        item = (await tenant_client.get("/credits/usage")).json()["logs"][0]
        assert item["billed_cost_usd"] == 0.02
        assert item["settlement_status"] == "charged"
        assert item["usage_metadata"] == {"k": "v"}

    async def test_it_filters_by_session(self, tenant_client, tenant, fake_supabase):
        self._seed(fake_supabase, tenant, n=2, session_id="s1")
        self._seed(fake_supabase, tenant, n=1, session_id="s2")
        r = await tenant_client.get("/credits/usage", params={"session_id": "s2"})
        assert r.json()["count"] == 1

    async def test_it_paginates(self, tenant_client, tenant, fake_supabase):
        self._seed(fake_supabase, tenant, n=5)
        assert (await tenant_client.get("/credits/usage", params={"limit": 2})).json()["count"] == 2

    async def test_it_is_scoped_to_the_caller(self, other_tenant_client, tenant, fake_supabase):
        self._seed(fake_supabase, tenant)
        assert (await other_tenant_client.get("/credits/usage")).json()["count"] == 0

    async def test_it_requires_auth(self, client):
        assert (await client.get("/credits/usage")).status_code == 401


class TestReconciliation:
    async def test_it_returns_a_report(self, tenant_client, tenant, fake_supabase):
        fake_supabase.db.seed(
            "credit_usage_logs",
            {
                "user_id": tenant.user_id,
                "session_id": "s1",
                "model_type": "llm",
                "model_id": "openai/gpt-4o",
                "units_used": 100,
                "cost_usd": 0.01,
                "credits_charged": 10,
            },
        )
        r = await tenant_client.get("/credits/reconciliation")
        assert r.status_code == 200
        body = r.json()
        assert body["summary"]["usage_rows"] == 1
        assert "provider_breakdown" in body
        assert "session_breakdown" in body
        assert "anomalies" in body

    async def test_an_empty_ledger_reports_zeroes(self, tenant_client):
        body = (await tenant_client.get("/credits/reconciliation")).json()
        assert body["summary"]["usage_rows"] == 0

    async def test_it_accepts_a_session_and_a_time_window(self, tenant_client):
        r = await tenant_client.get(
            "/credits/reconciliation",
            params={
                "session_id": "s1",
                "created_after": "2026-03-01T00:00:00Z",
                "created_before": "2026-03-31T00:00:00Z",
            },
        )
        assert r.status_code == 200

    @pytest.mark.parametrize("limit", [0, 2001])
    async def test_an_out_of_range_limit_is_a_422(self, tenant_client, limit):
        r = await tenant_client.get("/credits/reconciliation", params={"limit": limit})
        assert r.status_code == 422

    async def test_a_malformed_timestamp_is_a_422(self, tenant_client):
        r = await tenant_client.get(
            "/credits/reconciliation", params={"created_after": "not-a-date"}
        )
        assert r.status_code == 422

    async def test_it_requires_auth(self, client):
        assert (await client.get("/credits/reconciliation")).status_code == 401


class TestWebhookRouteGuards:
    async def test_a_missing_signature_header_is_a_400(self, client):
        r = await client.post("/credits/webhook", content=b"{}")
        assert r.status_code == 400
        assert r.json()["detail"] == "Missing stripe-signature header"

    async def test_a_runtime_error_is_a_500_without_upstream_text(
        self, monkeypatch, client, caplog
    ):
        async def boom(payload, sig):
            raise RuntimeError("STRIPE_WEBHOOK_SECRET must be set")

        monkeypatch.setattr(credits_route, "handle_webhook_event", boom)
        with caplog.at_level("ERROR", logger="kwami-api.credits"):
            r = await client.post(
                "/credits/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"}
            )
        assert r.status_code == 500
        assert r.json()["detail"] == "Webhook processing failed"
        assert "STRIPE_WEBHOOK_SECRET" not in r.text
        assert "Webhook processing error" in caplog.text


class TestKwamiApiKeyGuard:
    """`_verify_kwami_api_key` — the agent's shared secret for `/credits/report`.

    The replay and charging behaviour is in test_usage_report_endpoint.py; this
    is the guard itself, including the arm that distinguishes "the operator never
    configured a key" (503) from "the caller sent the wrong one" (401).
    """

    ENDPOINT = "/credits/usage/report"
    BODY = {"session_id": "s1", "usage": []}

    async def test_an_unconfigured_key_is_a_503(self, monkeypatch, client, tenant):
        monkeypatch.setattr(credits_route.settings, "kwami_api_key", None, raising=False)
        r = await client.post(
            self.ENDPOINT,
            json={**self.BODY, "user_id": tenant.user_id},
            headers={"X-API-Key": "anything"},
        )
        assert r.status_code == 503
        assert "not configured" in r.json()["detail"]

    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_a_blank_configured_key_is_also_a_503(self, monkeypatch, client, tenant, blank):
        """A whitespace-only KWAMI_API_KEY in a .env must not authenticate anyone."""
        monkeypatch.setattr(credits_route.settings, "kwami_api_key", blank, raising=False)
        r = await client.post(
            self.ENDPOINT,
            json={**self.BODY, "user_id": tenant.user_id},
            headers={"X-API-Key": blank},
        )
        assert r.status_code == 503

    async def test_a_missing_header_is_a_401(self, monkeypatch, client, tenant):
        monkeypatch.setattr(credits_route.settings, "kwami_api_key", "secret", raising=False)
        r = await client.post(self.ENDPOINT, json={**self.BODY, "user_id": tenant.user_id})
        assert r.status_code == 401

    @pytest.mark.parametrize("wrong", ["sec", "secrets", "Secret"])
    async def test_a_wrong_key_is_a_401_including_a_prefix(
        self, monkeypatch, client, tenant, wrong
    ):
        monkeypatch.setattr(credits_route.settings, "kwami_api_key", "secret", raising=False)
        r = await client.post(
            self.ENDPOINT,
            json={**self.BODY, "user_id": tenant.user_id},
            headers={"X-API-Key": wrong},
        )
        assert r.status_code == 401

    async def test_the_right_key_passes_the_guard(self, monkeypatch, client, tenant):
        monkeypatch.setattr(credits_route.settings, "kwami_api_key", "secret", raising=False)
        r = await client.post(
            self.ENDPOINT,
            json={**self.BODY, "user_id": tenant.user_id},
            headers={"X-API-Key": "secret"},
        )
        assert r.status_code == 200
