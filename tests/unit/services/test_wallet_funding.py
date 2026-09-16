"""Wallet funding must credit exactly once, and must be retry-safe.

The original flow lost money on every single deposit:
  1. the intent was marked 'confirmed',
  2. ... then `add_credits(transaction_type="wallet_funding")` was called, with a
     value absent from the `credit_transaction_type` enum, so the RPC raised,
  3. ... and the retry short-circuited on `status == "confirmed"` and returned
     "already_confirmed" without ever crediting.

Money in, credits never granted, no path back. These tests pin the fixed order
(credit first, confirm last) and the ledger-keyed idempotency check.
"""

from decimal import Decimal

import pytest

from src.services import wallet_service
from src.services.wallet_service import _compute_credit_amount

# --------------------------------------------------------------------------
# credit computation
# --------------------------------------------------------------------------


def test_a_usd_quote_is_used_when_present():
    assert _compute_credit_amount(Decimal("3"), Decimal("250"), asset_symbol="SOL") == 250_000_000


def test_stablecoins_need_no_quote():
    """1 USDC is a dollar, so the received amount is the USD value."""
    assert _compute_credit_amount(Decimal("10"), None, asset_symbol="USDC") == 10_000_000


def test_non_stablecoin_without_a_quote_is_refused():
    """The old fallback was `usd = amount`, crediting 1 SOL as one dollar."""
    with pytest.raises(ValueError, match="USD quote"):
        _compute_credit_amount(Decimal("3"), None, asset_symbol="SOL")


def test_unknown_asset_without_a_quote_is_refused():
    with pytest.raises(ValueError, match="USD quote"):
        _compute_credit_amount(Decimal("1000"), None, asset_symbol=None)


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------


@pytest.fixture
def funding_intent(fake_supabase, tenant):
    """A pending USDC funding intent for a tenant with a wallet."""
    wallet = fake_supabase.db.seed(
        "kwami_wallets",
        {
            "user_id": tenant.user_id,
            "kwami_id": tenant.kwami_id,
            "public_key": "mock_pubkey_funding",
            "status": "active",
        },
    )[0]
    return fake_supabase.db.seed(
        "wallet_funding_intents",
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "user_id": tenant.user_id,
            "kwami_id": tenant.kwami_id,
            "wallet_id": wallet["id"],
            "provider": "phantom_transfer",
            "status": "pending",
            "asset_symbol": "USDC",
            "asset_mint": "EPjFWdd5",
            "expected_amount": "25",
            "idempotency_key": "client-key-1",
        },
    )[0]


@pytest.mark.anyio
async def test_settlement_grants_credits_and_then_confirms(fake_supabase, funding_intent, tenant):
    result = await wallet_service.settle_funding_intent(
        "phantom_transfer",
        {"intent_id": funding_intent["id"], "amount_received": "25", "event_id": "evt-1"},
    )

    assert result["status"] == "confirmed"
    assert result["credits_added_micro"] == 25_000_000

    ledger = fake_supabase.db.rows("credit_transactions")
    assert len(ledger) == 1
    # 'wallet_funding' is not a member of credit_transaction_type; using it is
    # what made the RPC raise on every deposit.
    assert ledger[0]["type"] == "purchase"
    assert ledger[0]["metadata"]["source"] == "wallet"
    assert ledger[0]["metadata"]["intent_id"] == funding_intent["id"]

    intent = fake_supabase.db.rows("wallet_funding_intents")[0]
    assert intent["status"] == "confirmed"


@pytest.mark.anyio
async def test_replaying_the_same_settlement_does_not_double_credit(fake_supabase, funding_intent):
    payload = {"intent_id": funding_intent["id"], "amount_received": "25", "event_id": "evt-1"}
    first = await wallet_service.settle_funding_intent("phantom_transfer", payload)
    second = await wallet_service.settle_funding_intent("phantom_transfer", payload)

    assert first["status"] == "confirmed"
    assert second["status"] in {"already_confirmed", "duplicate"}

    balance = fake_supabase.db.rows("user_credits")[0]["balance"]
    assert balance == 500_000 + 25_000_000, "the deposit must be credited exactly once"
    assert len(fake_supabase.db.rows("credit_transactions")) == 1


@pytest.mark.anyio
async def test_a_confirmed_but_uncredited_intent_is_repaired_on_retry(
    fake_supabase, funding_intent
):
    """The exact state the bug left every deposit in.

    Previously this returned "already_confirmed" and the user never received
    their credits. Because the check now keys on the ledger, the retry credits.
    """
    fake_supabase.db.rows("wallet_funding_intents")[0]["status"] = "confirmed"
    assert fake_supabase.db.rows("credit_transactions") == []

    result = await wallet_service.settle_funding_intent(
        "phantom_transfer",
        {"intent_id": funding_intent["id"], "amount_received": "25", "event_id": "evt-retry"},
    )

    assert result["status"] == "confirmed"
    assert result["credits_added_micro"] == 25_000_000
    assert len(fake_supabase.db.rows("credit_transactions")) == 1
