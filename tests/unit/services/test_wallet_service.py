"""`src.services.wallet_service` and `/wallets` — the paths the existing files leave.

tests/unit/services/test_wallet_funding.py covers credit computation and the
credit-then-confirm ordering; tests/api/test_wallet.py covers three happy-path
routes. This covers the rest: allowlisting, intent construction and its
rejections, the settlement guards, and every route's failure arms including the
feature flag.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from src.services import wallet_service
from src.services.wallet_service import (
    SOL_MINT,
    _compute_credit_amount,
    _fetch_allowlist,
    _fetch_wallet,
    _is_allowed_mint,
    _resolve_owned_kwami,
    add_custom_allowlist_token,
    create_funding_intent,
    create_kwami_wallet,
    get_kwami_wallet_overview,
    settle_funding_intent,
    verify_wallet_webhook_signature,
)


@pytest.fixture(autouse=True)
def _wallet_on(monkeypatch):
    monkeypatch.setattr(wallet_service.settings, "wallet_enabled", True, raising=False)
    from src.api.routes import wallet as wallet_route

    monkeypatch.setattr(wallet_route.settings, "wallet_enabled", True, raising=False)


@pytest.fixture
def allowlisted(fake_supabase):
    fake_supabase.db.seed(
        "wallet_token_allowlist",
        {
            "chain": "solana",
            "mint_address": SOL_MINT,
            "symbol": "SOL",
            "decimals": 9,
            "is_stablecoin": False,
            "is_default": True,
            "created_by_user_id": None,
        },
    )


# -- helpers -----------------------------------------------------------------


class TestResolveOwnedKwami:
    @pytest.mark.anyio
    async def test_it_finds_an_owned_kwami(self, fake_supabase, tenant):
        assert (await _resolve_owned_kwami(tenant.user_id, tenant.kwami_id))[
            "id"
        ] == tenant.kwami_id

    @pytest.mark.anyio
    async def test_another_tenants_kwami_is_not_found(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(ValueError, match="Kwami not found"):
            await _resolve_owned_kwami(other_tenant.user_id, tenant.kwami_id)


class TestFetchWallet:
    @pytest.mark.anyio
    async def test_no_wallet_is_none(self, fake_supabase, tenant):
        assert await _fetch_wallet(tenant.user_id, tenant.kwami_id) is None

    @pytest.mark.anyio
    async def test_a_created_wallet_is_found(self, fake_supabase, tenant):
        await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        assert await _fetch_wallet(tenant.user_id, tenant.kwami_id) is not None


class TestFetchAllowlist:
    @pytest.mark.anyio
    async def test_defaults_are_visible_to_everyone(self, fake_supabase, tenant, allowlisted):
        assert [r["symbol"] for r in await _fetch_allowlist(tenant.user_id)] == ["SOL"]

    @pytest.mark.anyio
    async def test_a_users_own_custom_token_is_visible(self, fake_supabase, tenant):
        fake_supabase.db.seed(
            "wallet_token_allowlist",
            {
                "chain": "solana",
                "mint_address": "M" * 32,
                "symbol": "MINE",
                "decimals": 6,
                "is_stablecoin": False,
                "is_default": False,
                "created_by_user_id": tenant.user_id,
            },
        )
        assert [r["symbol"] for r in await _fetch_allowlist(tenant.user_id)] == ["MINE"]

    @pytest.mark.anyio
    async def test_another_users_custom_token_is_hidden(self, fake_supabase, tenant, other_tenant):
        fake_supabase.db.seed(
            "wallet_token_allowlist",
            {
                "chain": "solana",
                "mint_address": "M" * 32,
                "symbol": "THEIRS",
                "decimals": 6,
                "is_stablecoin": False,
                "is_default": False,
                "created_by_user_id": other_tenant.user_id,
            },
        )
        assert await _fetch_allowlist(tenant.user_id) == []


class TestIsAllowedMint:
    @pytest.mark.anyio
    async def test_an_allowlisted_mint_passes(self, fake_supabase, tenant, allowlisted):
        assert await _is_allowed_mint(tenant.user_id, SOL_MINT) is True

    @pytest.mark.anyio
    async def test_surrounding_whitespace_is_ignored(self, fake_supabase, tenant, allowlisted):
        assert await _is_allowed_mint(tenant.user_id, f"  {SOL_MINT}  ") is True

    @pytest.mark.anyio
    async def test_an_unknown_mint_is_refused(self, fake_supabase, tenant, allowlisted):
        assert await _is_allowed_mint(tenant.user_id, "X" * 44) is False


class TestComputeCreditAmount:
    def test_a_zero_usd_quote_falls_through_to_the_symbol_rule(self):
        """`amount_usd > 0` -- a zero quote is not a quote."""
        assert _compute_credit_amount(Decimal("5"), Decimal("0"), asset_symbol="USDC") == 5_000_000
        with pytest.raises(ValueError, match="USD quote"):
            _compute_credit_amount(Decimal("5"), Decimal("0"), asset_symbol="SOL")

    def test_the_stablecoin_check_is_case_and_space_insensitive(self):
        assert _compute_credit_amount(Decimal("1"), None, asset_symbol="  usdc  ") == 1_000_000

    def test_a_dust_deposit_still_grants_one_micro_credit(self):
        assert _compute_credit_amount(Decimal("0.0000001"), None, asset_symbol="USDC") == 1


class TestVerifyWalletWebhookSignature:
    def test_no_configured_secret_refuses_everything(self, monkeypatch):
        monkeypatch.setattr(wallet_service.settings, "wallet_webhook_secret", None, raising=False)
        assert verify_wallet_webhook_signature(b"{}", "anything") is False

    def test_a_missing_signature_is_refused(self, monkeypatch):
        monkeypatch.setattr(wallet_service.settings, "wallet_webhook_secret", "s", raising=False)
        assert verify_wallet_webhook_signature(b"{}", None) is False
        assert verify_wallet_webhook_signature(b"{}", "") is False

    def test_a_correct_signature_is_accepted(self, monkeypatch):
        monkeypatch.setattr(wallet_service.settings, "wallet_webhook_secret", "shh", raising=False)
        body = b'{"a":1}'
        sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        assert verify_wallet_webhook_signature(body, sig) is True

    def test_a_signature_over_different_bytes_is_refused(self, monkeypatch):
        monkeypatch.setattr(wallet_service.settings, "wallet_webhook_secret", "shh", raising=False)
        sig = hmac.new(b"shh", b'{"a":1}', hashlib.sha256).hexdigest()
        assert verify_wallet_webhook_signature(b'{"a":2}', sig) is False


# -- service -----------------------------------------------------------------


class TestCreateKwamiWallet:
    @pytest.mark.anyio
    async def test_it_provisions_a_wallet_and_a_key_ref(self, fake_supabase, tenant):
        wallet = await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        assert wallet["chain"] == "solana"
        assert wallet["status"] == "active"
        assert wallet["public_key"]
        assert len(fake_supabase.db.rows("kwami_wallet_key_refs")) == 1

    @pytest.mark.anyio
    async def test_the_private_key_never_reaches_the_wallet_row(self, fake_supabase, tenant):
        wallet = await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        assert "private" not in json.dumps(wallet).lower()

    @pytest.mark.anyio
    async def test_it_is_idempotent(self, fake_supabase, tenant):
        first = await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        second = await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        assert second["id"] == first["id"]
        assert len(fake_supabase.db.rows("kwami_wallets")) == 1

    @pytest.mark.anyio
    async def test_another_tenants_kwami_is_refused(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(ValueError, match="Kwami not found"):
            await create_kwami_wallet(other_tenant.user_id, tenant.kwami_id)


class TestGetKwamiWalletOverview:
    @pytest.mark.anyio
    async def test_no_wallet_yields_an_empty_overview(self, fake_supabase, tenant, allowlisted):
        overview = await get_kwami_wallet_overview(tenant.user_id, tenant.kwami_id)
        assert overview["wallet"] is None
        assert overview["balances"] == []
        assert overview["transactions"] == []
        assert [r["symbol"] for r in overview["allowlist"]] == ["SOL"]
        assert "funding_intents" not in overview, "the empty shape omits it"

    @pytest.mark.anyio
    async def test_a_provisioned_wallet_yields_every_section(
        self, fake_supabase, tenant, allowlisted
    ):
        wallet = await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        fake_supabase.db.seed(
            "wallet_balances_cache",
            {
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "wallet_id": wallet["id"],
                "mint_address": SOL_MINT,
                "symbol": "SOL",
                "amount": "1",
            },
        )
        fake_supabase.db.seed(
            "wallet_transactions",
            {
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "wallet_id": wallet["id"],
                "mint_address": SOL_MINT,
                "symbol": "SOL",
                "direction": "in",
                "amount": "1",
            },
        )
        overview = await get_kwami_wallet_overview(tenant.user_id, tenant.kwami_id)
        assert overview["wallet"]["id"] == wallet["id"]
        assert len(overview["balances"]) == 1
        assert len(overview["transactions"]) == 1
        assert overview["funding_intents"] == []

    @pytest.mark.anyio
    async def test_another_tenants_kwami_is_refused(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(ValueError, match="Kwami not found"):
            await get_kwami_wallet_overview(other_tenant.user_id, tenant.kwami_id)


class TestAddCustomAllowlistToken:
    @pytest.mark.anyio
    async def test_it_normalises_and_stores(self, fake_supabase, tenant):
        token = await add_custom_allowlist_token(
            tenant.user_id,
            mint_address=f"  {'M' * 32}  ",
            symbol="  mine  ",
            decimals=6,
            is_stablecoin=True,
        )
        assert token["mint_address"] == "M" * 32
        assert token["symbol"] == "MINE"
        assert token["is_default"] is False
        assert token["created_by_user_id"] == tenant.user_id

    @pytest.mark.anyio
    async def test_a_short_mint_is_refused(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Invalid mint address"):
            await add_custom_allowlist_token(
                tenant.user_id,
                mint_address="short",
                symbol="S",
                decimals=6,
                is_stablecoin=False,
            )

    @pytest.mark.anyio
    @pytest.mark.parametrize("symbol", ["", "   ", "X" * 13])
    async def test_a_bad_symbol_is_refused(self, fake_supabase, tenant, symbol):
        with pytest.raises(ValueError, match="Invalid token symbol"):
            await add_custom_allowlist_token(
                tenant.user_id,
                mint_address="M" * 32,
                symbol=symbol,
                decimals=6,
                is_stablecoin=False,
            )

    @pytest.mark.anyio
    async def test_an_insert_returning_nothing_falls_back_to_the_payload(
        self, monkeypatch, fake_supabase, tenant
    ):
        class _T:
            def insert(self, payload):
                return self

            async def execute(self):
                return type("R", (), {"data": []})()

        monkeypatch.setattr(
            wallet_service,
            "get_supabase_admin",
            lambda: type("C", (), {"table": staticmethod(lambda n: _T())})(),
        )
        token = await add_custom_allowlist_token(
            tenant.user_id, mint_address="M" * 32, symbol="MINE", decimals=6, is_stablecoin=False
        )
        assert token["symbol"] == "MINE"


class TestCreateFundingIntent:
    async def _wallet(self, tenant):
        return await create_kwami_wallet(tenant.user_id, tenant.kwami_id)

    @pytest.mark.anyio
    async def test_a_phantom_intent_has_no_redirect(self, fake_supabase, tenant, allowlisted):
        await self._wallet(tenant)
        intent = await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="sol",
            amount=Decimal("1"),
            amount_usd=Decimal("250"),
            sender_wallet_pubkey="SENDER",
        )
        assert intent["provider_redirect_url"] is None
        assert intent["status"] == "pending"
        assert intent["asset_symbol"] == "SOL", "symbols are upper-cased"
        assert intent["sender_wallet_pubkey"] == "SENDER"

    @pytest.mark.anyio
    async def test_a_card_intent_carries_a_redirect(
        self, monkeypatch, fake_supabase, tenant, allowlisted
    ):
        monkeypatch.setattr(
            wallet_service.settings,
            "wallet_card_provider_base_url",
            "https://buy.example.com/",
            raising=False,
        )
        await self._wallet(tenant)
        intent = await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="card_provider",
            asset_mint=SOL_MINT,
            asset_symbol="SOL",
            amount=Decimal("1"),
            amount_usd=None,
            sender_wallet_pubkey=None,
        )
        assert intent["provider_redirect_url"] == (
            f"https://buy.example.com/buy?intent={intent['id']}"
        )

    @pytest.mark.anyio
    async def test_it_records_an_intent_created_event(self, fake_supabase, tenant, allowlisted):
        await self._wallet(tenant)
        await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="SOL",
            amount=Decimal("1"),
            amount_usd=None,
            sender_wallet_pubkey=None,
        )
        (event,) = fake_supabase.db.rows("wallet_funding_events")
        assert event["event_type"] == "intent_created"

    @pytest.mark.anyio
    async def test_the_same_idempotency_key_returns_the_first_intent(
        self, fake_supabase, tenant, allowlisted
    ):
        await self._wallet(tenant)
        kwargs = dict(
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="SOL",
            amount=Decimal("1"),
            amount_usd=None,
            sender_wallet_pubkey=None,
            idempotency_key="  key-1  ",
        )
        first = await create_funding_intent(tenant.user_id, **kwargs)
        second = await create_funding_intent(tenant.user_id, **kwargs)
        assert second["id"] == first["id"]
        assert len(fake_supabase.db.rows("wallet_funding_intents")) == 1

    @pytest.mark.anyio
    async def test_an_absent_key_is_derived(self, fake_supabase, tenant, allowlisted):
        await self._wallet(tenant)
        intent = await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="SOL",
            amount=Decimal("1"),
            amount_usd=None,
            sender_wallet_pubkey=None,
            idempotency_key="   ",
        )
        assert len(intent["idempotency_key"]) == 64

    @pytest.mark.anyio
    async def test_an_unsupported_provider_is_refused(self, fake_supabase, tenant, allowlisted):
        with pytest.raises(ValueError, match="Unsupported funding provider"):
            await create_funding_intent(
                tenant.user_id,
                kwami_id=tenant.kwami_id,
                provider="wire_transfer",
                asset_mint=SOL_MINT,
                asset_symbol="SOL",
                amount=Decimal("1"),
                amount_usd=None,
                sender_wallet_pubkey=None,
            )

    @pytest.mark.anyio
    @pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1")])
    async def test_a_non_positive_amount_is_refused(
        self, fake_supabase, tenant, allowlisted, amount
    ):
        with pytest.raises(ValueError, match="greater than zero"):
            await create_funding_intent(
                tenant.user_id,
                kwami_id=tenant.kwami_id,
                provider="phantom_transfer",
                asset_mint=SOL_MINT,
                asset_symbol="SOL",
                amount=amount,
                amount_usd=None,
                sender_wallet_pubkey=None,
            )

    @pytest.mark.anyio
    async def test_a_non_allowlisted_mint_is_refused(self, fake_supabase, tenant, allowlisted):
        with pytest.raises(ValueError, match="not allowlisted"):
            await create_funding_intent(
                tenant.user_id,
                kwami_id=tenant.kwami_id,
                provider="phantom_transfer",
                asset_mint="X" * 44,
                asset_symbol="XXX",
                amount=Decimal("1"),
                amount_usd=None,
                sender_wallet_pubkey=None,
            )

    @pytest.mark.anyio
    async def test_funding_before_the_wallet_exists_is_refused(
        self, fake_supabase, tenant, allowlisted
    ):
        with pytest.raises(ValueError, match="Create a wallet before funding"):
            await create_funding_intent(
                tenant.user_id,
                kwami_id=tenant.kwami_id,
                provider="phantom_transfer",
                asset_mint=SOL_MINT,
                asset_symbol="SOL",
                amount=Decimal("1"),
                amount_usd=None,
                sender_wallet_pubkey=None,
            )


class TestSettleFundingIntentGuards:
    @pytest.mark.anyio
    async def test_an_unsupported_provider_is_refused(self, fake_supabase):
        with pytest.raises(ValueError, match="Unsupported funding provider"):
            await settle_funding_intent("wire", {"intent_id": "x"})

    @pytest.mark.anyio
    @pytest.mark.parametrize("payload", [{}, {"intent_id": ""}, {"intent_id": "   "}])
    async def test_a_missing_intent_id_is_refused(self, fake_supabase, payload):
        with pytest.raises(ValueError, match="intent_id is required"):
            await settle_funding_intent("phantom_transfer", payload)

    @pytest.mark.anyio
    async def test_an_unknown_intent_is_refused(self, fake_supabase):
        with pytest.raises(ValueError, match="Funding intent not found"):
            await settle_funding_intent(
                "phantom_transfer", {"intent_id": "00000000-0000-0000-0000-000000000000"}
            )

    @pytest.mark.anyio
    async def test_a_replayed_provider_event_is_a_duplicate(
        self, fake_supabase, tenant, allowlisted
    ):
        """Keyed on the provider's own event id, before any money moves."""
        await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        intent = await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="USDC",
            amount=Decimal("1"),
            amount_usd=None,
            sender_wallet_pubkey=None,
        )
        fake_supabase.db.seed(
            "wallet_funding_events",
            {
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "wallet_id": intent["wallet_id"],
                "intent_id": intent["id"],
                "event_type": "confirmed",
                "provider": "phantom_transfer",
                "provider_event_id": "evt-1",
            },
        )
        result = await settle_funding_intent(
            "phantom_transfer", {"intent_id": intent["id"], "event_id": "evt-1"}
        )
        assert result == {"status": "duplicate", "intent_id": intent["id"]}

    @pytest.mark.anyio
    async def test_the_expected_amount_is_used_when_the_provider_sends_none(
        self, fake_supabase, tenant, allowlisted
    ):
        await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        intent = await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="USDC",
            amount=Decimal("7"),
            amount_usd=None,
            sender_wallet_pubkey=None,
        )
        result = await settle_funding_intent("phantom_transfer", {"intent_id": intent["id"]})
        assert result["status"] == "confirmed"
        assert result["credits_added_micro"] == 7_000_000

    @pytest.mark.anyio
    async def test_a_transaction_signature_is_generated_when_absent(
        self, fake_supabase, tenant, allowlisted
    ):
        await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        intent = await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="USDC",
            amount=Decimal("1"),
            amount_usd=None,
            sender_wallet_pubkey=None,
        )
        await settle_funding_intent("phantom_transfer", {"intent_id": intent["id"]})
        (tx,) = fake_supabase.db.rows("wallet_transactions")
        assert tx["transaction_signature"].startswith("phantom_transfer-")


# -- routes ------------------------------------------------------------------


class TestWalletRoutes:
    @pytest.mark.anyio
    async def test_every_route_503s_when_the_feature_is_off(
        self, monkeypatch, tenant_client, tenant
    ):
        from src.api.routes import wallet as wallet_route

        monkeypatch.setattr(wallet_route.settings, "wallet_enabled", False, raising=False)
        calls = [
            tenant_client.post(f"/wallets/kwamis/{tenant.kwami_id}"),
            tenant_client.get(f"/wallets/kwamis/{tenant.kwami_id}"),
            tenant_client.post(
                f"/wallets/kwamis/{tenant.kwami_id}/fund/phantom-intent",
                json={"assetMint": SOL_MINT, "assetSymbol": "SOL", "amount": "1"},
            ),
            tenant_client.post(
                f"/wallets/kwamis/{tenant.kwami_id}/fund/card-intent",
                json={"assetMint": SOL_MINT, "assetSymbol": "SOL", "amount": "1"},
            ),
            tenant_client.post(
                "/wallets/allowlist",
                json={"mintAddress": "M" * 32, "symbol": "M", "decimals": 6},
            ),
            tenant_client.post("/wallets/webhooks/phantom_transfer", content=b"{}"),
        ]
        for call in calls:
            r = await call
            assert r.status_code == 503
            assert r.json()["detail"] == "Wallet feature is disabled"

    @pytest.mark.anyio
    async def test_creating_a_wallet_for_another_tenants_kwami_is_a_404(
        self, other_tenant_client, tenant
    ):
        r = await other_tenant_client.post(f"/wallets/kwamis/{tenant.kwami_id}")
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_a_custody_failure_is_a_503(self, monkeypatch, tenant_client, tenant):
        from src.api.routes import wallet as wallet_route
        from src.services.custody_service import CustodyError

        async def boom(user_id, kwami_id):
            raise CustodyError("Wallet custody provider is not configured")

        monkeypatch.setattr(wallet_route, "create_kwami_wallet", boom)
        r = await tenant_client.post(f"/wallets/kwamis/{tenant.kwami_id}")
        assert r.status_code == 503
        assert "custody provider" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_the_overview_for_another_tenants_kwami_is_a_404(
        self, other_tenant_client, tenant
    ):
        assert (
            await other_tenant_client.get(f"/wallets/kwamis/{tenant.kwami_id}")
        ).status_code == 404

    @pytest.mark.anyio
    async def test_a_card_intent_is_created(
        self, tenant_client, tenant, fake_supabase, allowlisted
    ):
        await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        r = await tenant_client.post(
            f"/wallets/kwamis/{tenant.kwami_id}/fund/card-intent",
            json={"assetMint": SOL_MINT, "assetSymbol": "SOL", "amount": "1", "amountUsd": "250"},
        )
        assert r.status_code == 200
        assert r.json()["intent"]["provider"] == "card_provider"

    @pytest.mark.anyio
    async def test_a_rejected_phantom_intent_is_a_400(self, tenant_client, tenant, allowlisted):
        r = await tenant_client.post(
            f"/wallets/kwamis/{tenant.kwami_id}/fund/phantom-intent",
            json={"assetMint": SOL_MINT, "assetSymbol": "SOL", "amount": "0"},
        )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_a_rejected_card_intent_is_a_400(self, tenant_client, tenant, allowlisted):
        r = await tenant_client.post(
            f"/wallets/kwamis/{tenant.kwami_id}/fund/card-intent",
            json={"assetMint": SOL_MINT, "assetSymbol": "SOL", "amount": "-1"},
        )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_a_token_can_be_allowlisted(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/wallets/allowlist",
            json={"mintAddress": "M" * 32, "symbol": "MINE", "decimals": 6, "isStablecoin": True},
        )
        assert r.status_code == 200
        assert r.json()["token"]["symbol"] == "MINE"

    @pytest.mark.anyio
    @pytest.mark.parametrize("decimals", [-1, 19])
    async def test_out_of_range_decimals_are_a_422(self, tenant_client, decimals):
        r = await tenant_client.post(
            "/wallets/allowlist",
            json={"mintAddress": "M" * 32, "symbol": "MINE", "decimals": decimals},
        )
        assert r.status_code == 422

    @pytest.mark.anyio
    async def test_an_unsigned_webhook_is_a_401(self, monkeypatch, client):
        monkeypatch.setattr(wallet_service.settings, "wallet_webhook_secret", "shh", raising=False)
        r = await client.post("/wallets/webhooks/phantom_transfer", content=b"{}")
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid wallet webhook signature"

    @pytest.mark.anyio
    async def test_a_signed_webhook_for_an_unknown_intent_is_a_400(self, monkeypatch, client):
        monkeypatch.setattr(wallet_service.settings, "wallet_webhook_secret", "shh", raising=False)
        body = json.dumps({"intent_id": "00000000-0000-0000-0000-000000000000"}).encode()
        sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        r = await client.post(
            "/wallets/webhooks/phantom_transfer",
            content=body,
            headers={"X-Wallet-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert "Funding intent not found" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_a_signed_webhook_settles(
        self, monkeypatch, client, fake_supabase, tenant, allowlisted
    ):
        monkeypatch.setattr(wallet_service.settings, "wallet_webhook_secret", "shh", raising=False)
        await create_kwami_wallet(tenant.user_id, tenant.kwami_id)
        intent = await create_funding_intent(
            tenant.user_id,
            kwami_id=tenant.kwami_id,
            provider="phantom_transfer",
            asset_mint=SOL_MINT,
            asset_symbol="USDC",
            amount=Decimal("5"),
            amount_usd=None,
            sender_wallet_pubkey=None,
        )
        body = json.dumps({"intent_id": intent["id"]}).encode()
        sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        r = await client.post(
            "/wallets/webhooks/phantom_transfer",
            content=body,
            headers={"X-Wallet-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "confirmed"

    @pytest.mark.anyio
    async def test_the_routes_require_auth(self, client, tenant):
        assert (await client.post(f"/wallets/kwamis/{tenant.kwami_id}")).status_code == 401
        assert (await client.get(f"/wallets/kwamis/{tenant.kwami_id}")).status_code == 401
        assert (await client.post("/wallets/allowlist", json={})).status_code == 401
