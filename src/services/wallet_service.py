"""Wallet service for kwami Solana wallets."""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from src.core.config import settings
from src.services.credits import MICRO_CREDITS_PER_CREDIT, add_credits, get_supabase_admin
from src.services.custody_service import custody_service

logger = logging.getLogger("kwami-api.wallet")

SOL_MINT = "So11111111111111111111111111111111111111112"
SUPPORTED_FUNDING_PROVIDERS = {"phantom_transfer", "card_provider"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_owned_kwami(user_id: str, kwami_id: str) -> dict[str, Any]:
    sb = get_supabase_admin()
    result = (
        sb.table("user_kwamis")
        .select("id,user_id,name")
        .eq("id", kwami_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise ValueError("Kwami not found")
    return rows[0]


def _fetch_wallet(user_id: str, kwami_id: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    result = (
        sb.table("kwami_wallets")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def _fetch_allowlist(user_id: str) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    result = (
        sb.table("wallet_token_allowlist")
        .select("*")
        .order("is_default", desc=True)
        .order("symbol")
        .execute()
    )
    rows = result.data or []
    return [
        row
        for row in rows
        if row.get("is_default") or row.get("created_by_user_id") in (None, user_id)
    ]


def _is_allowed_mint(user_id: str, mint_address: str) -> bool:
    allowlist = _fetch_allowlist(user_id)
    wanted = mint_address.strip()
    return any(row.get("mint_address") == wanted for row in allowlist)


# Mints whose unit is a US dollar, so the received amount *is* the USD value.
STABLECOIN_SYMBOLS = frozenset({"USDC", "USDT", "PYUSD", "USDP", "DAI"})


def _compute_credit_amount(
    amount: Decimal,
    amount_usd: Decimal | None,
    *,
    asset_symbol: str | None = None,
) -> int:
    """Credits to grant for a confirmed deposit.

    The fallback used to be `usd = amount`, treating the raw token amount as
    dollars. For anything that is not a dollar-denominated stablecoin that is
    simply wrong -- a 1 SOL deposit was credited as $1 -- so a quote is now
    required unless the asset is a known stablecoin.
    """
    if amount_usd is not None and amount_usd > 0:
        usd = amount_usd
    elif asset_symbol and asset_symbol.strip().upper() in STABLECOIN_SYMBOLS:
        usd = amount
    else:
        raise ValueError("A USD quote (amount_usd) is required to credit a non-stablecoin deposit")
    credits = int((usd * Decimal(1000)).quantize(Decimal("1")))
    return max(credits * MICRO_CREDITS_PER_CREDIT, 1)


def _already_credited(user_id: str, intent_id: str) -> bool:
    """Has this intent already produced a ledger entry?

    Keyed on the ledger, not on `wallet_funding_intents.status`. The status was
    written before crediting, so a confirmed-but-uncredited intent -- the state
    the enum bug left every funding in -- must be treated as still owing credits
    and retried, not short-circuited as done.
    """
    sb = get_supabase_admin()
    result = (
        sb.table("credit_transactions")
        .select("id")
        .eq("user_id", user_id)
        .eq("metadata->>intent_id", intent_id)
        .limit(1)
        .execute()
    )
    return bool(getattr(result, "data", None) or [])


async def create_kwami_wallet(user_id: str, kwami_id: str) -> dict[str, Any]:
    _resolve_owned_kwami(user_id, kwami_id)
    existing = _fetch_wallet(user_id, kwami_id)
    if existing:
        return existing

    material = custody_service.create_wallet_material(user_id=user_id, kwami_id=kwami_id)
    sb = get_supabase_admin()
    wallet_id = str(uuid.uuid4())
    now_iso = _now_iso()

    wallet_payload = {
        "id": wallet_id,
        "user_id": user_id,
        "kwami_id": kwami_id,
        "chain": "solana",
        "network": settings.wallet_network,
        "custody_type": "custodial_hsm_mpc",
        "status": "active",
        "public_key": material.public_key,
        "metadata": material.metadata,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    sb.table("kwami_wallets").insert(wallet_payload).execute()
    sb.table("kwami_wallet_key_refs").insert(
        {
            "wallet_id": wallet_id,
            "user_id": user_id,
            "kwami_id": kwami_id,
            "custody_provider": material.provider,
            "key_ref": material.key_ref,
            "key_version": 1,
            "encryption_context": {"kwami_id": kwami_id, "user_id": user_id},
            "metadata": {"provisioned_at": now_iso},
        }
    ).execute()

    return wallet_payload


async def get_kwami_wallet_overview(user_id: str, kwami_id: str) -> dict[str, Any]:
    _resolve_owned_kwami(user_id, kwami_id)
    wallet = _fetch_wallet(user_id, kwami_id)
    allowlist = _fetch_allowlist(user_id)
    if not wallet:
        return {"wallet": None, "balances": [], "allowlist": allowlist, "transactions": []}

    sb = get_supabase_admin()
    balances = (
        sb.table("wallet_balances_cache")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .order("updated_at", desc=True)
        .execute()
    ).data or []
    txs = (
        sb.table("wallet_transactions")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    ).data or []
    intents = (
        sb.table("wallet_funding_intents")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    ).data or []
    return {
        "wallet": wallet,
        "balances": balances,
        "allowlist": allowlist,
        "transactions": txs,
        "funding_intents": intents,
    }


async def add_custom_allowlist_token(
    user_id: str,
    *,
    mint_address: str,
    symbol: str,
    decimals: int,
    is_stablecoin: bool,
) -> dict[str, Any]:
    mint = mint_address.strip()
    symbol_norm = symbol.strip().upper()
    if len(mint) < 20:
        raise ValueError("Invalid mint address")
    if not symbol_norm or len(symbol_norm) > 12:
        raise ValueError("Invalid token symbol")

    sb = get_supabase_admin()
    payload = {
        "id": str(uuid.uuid4()),
        "chain": "solana",
        "mint_address": mint,
        "symbol": symbol_norm,
        "decimals": decimals,
        "is_stablecoin": bool(is_stablecoin),
        "is_default": False,
        "created_by_user_id": user_id,
        "created_at": _now_iso(),
    }
    result = sb.table("wallet_token_allowlist").insert(payload).execute()
    rows = result.data or []
    return rows[0] if rows else payload


async def create_funding_intent(
    user_id: str,
    *,
    kwami_id: str,
    provider: str,
    asset_mint: str,
    asset_symbol: str,
    amount: Decimal,
    amount_usd: Decimal | None,
    sender_wallet_pubkey: str | None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if provider not in SUPPORTED_FUNDING_PROVIDERS:
        raise ValueError("Unsupported funding provider")
    if amount <= 0:
        raise ValueError("Funding amount must be greater than zero")
    if not _is_allowed_mint(user_id, asset_mint):
        raise ValueError("Token mint is not allowlisted")

    wallet = _fetch_wallet(user_id, kwami_id)
    if not wallet:
        raise ValueError("Create a wallet before funding")

    sb = get_supabase_admin()
    idem_key = (idempotency_key or "").strip()
    if not idem_key:
        idem_input = f"{user_id}:{kwami_id}:{provider}:{asset_mint}:{amount}:{_now_iso()}"
        idem_key = hashlib.sha256(idem_input.encode("utf-8")).hexdigest()

    existing = (
        sb.table("wallet_funding_intents")
        .select("*")
        .eq("idempotency_key", idem_key)
        .limit(1)
        .execute()
    ).data or []
    if existing:
        return existing[0]

    intent_id = str(uuid.uuid4())
    expires_at = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    redirect_url = None
    if provider == "card_provider":
        redirect_url = (
            f"{settings.wallet_card_provider_base_url.rstrip('/')}/buy?intent={intent_id}"
        )
    payload = {
        "id": intent_id,
        "user_id": user_id,
        "kwami_id": kwami_id,
        "wallet_id": wallet["id"],
        "provider": provider,
        "status": "pending",
        "asset_mint": asset_mint,
        "asset_symbol": asset_symbol.upper(),
        "expected_amount": str(amount),
        "expected_amount_usd": str(amount_usd) if amount_usd is not None else None,
        "sender_wallet_pubkey": sender_wallet_pubkey,
        "destination_wallet_pubkey": wallet["public_key"],
        "provider_intent_id": None,
        "provider_redirect_url": redirect_url,
        "idempotency_key": idem_key,
        "expires_at": expires_at,
        "metadata": {"network": settings.wallet_network},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    sb.table("wallet_funding_intents").insert(payload).execute()
    sb.table("wallet_funding_events").insert(
        {
            "user_id": user_id,
            "kwami_id": kwami_id,
            "wallet_id": wallet["id"],
            "intent_id": intent_id,
            "event_type": "intent_created",
            "provider": provider,
            "payload": {"sender_wallet_pubkey": sender_wallet_pubkey},
            "created_at": _now_iso(),
        }
    ).execute()
    return payload


def verify_wallet_webhook_signature(payload: bytes, signature: str | None) -> bool:
    if not settings.wallet_webhook_secret:
        return False
    if not signature:
        return False
    expected = hmac.new(
        settings.wallet_webhook_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def settle_funding_intent(
    provider: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if provider not in SUPPORTED_FUNDING_PROVIDERS:
        raise ValueError("Unsupported funding provider")
    intent_id = str(payload.get("intent_id", "")).strip()
    if not intent_id:
        raise ValueError("intent_id is required")
    sb = get_supabase_admin()
    result = sb.table("wallet_funding_intents").select("*").eq("id", intent_id).limit(1).execute()
    rows = result.data or []
    if not rows:
        raise ValueError("Funding intent not found")
    intent = rows[0]
    # Deliberately NOT `if intent["status"] == "confirmed"`. The status was
    # written before crediting, so that check made a confirmed-but-uncredited
    # intent permanently unrecoverable -- which is the state every wallet funding
    # ended up in. Ownership of the answer belongs to the ledger.
    if _already_credited(intent["user_id"], intent_id):
        return {"status": "already_confirmed", "intent_id": intent_id}

    event_id = str(payload.get("event_id") or "")
    if event_id:
        dup = (
            sb.table("wallet_funding_events")
            .select("id")
            .eq("provider", provider)
            .eq("provider_event_id", event_id)
            .limit(1)
            .execute()
        ).data or []
        if dup:
            return {"status": "duplicate", "intent_id": intent_id}

    amount_received = Decimal(
        str(payload.get("amount_received") or intent.get("expected_amount") or "0")
    )
    amount_usd = payload.get("amount_usd")
    amount_usd_dec = Decimal(str(amount_usd)) if amount_usd is not None else None
    tx_sig = str(payload.get("transaction_signature") or f"{provider}-{uuid.uuid4().hex}")

    sb.table("wallet_funding_events").insert(
        {
            "user_id": intent["user_id"],
            "kwami_id": intent["kwami_id"],
            "wallet_id": intent["wallet_id"],
            "intent_id": intent["id"],
            "event_type": "confirmed",
            "provider": provider,
            "provider_event_id": event_id or None,
            "transaction_signature": tx_sig,
            "amount_received": str(amount_received),
            "confirmed_at": _now_iso(),
            "payload": payload,
            "created_at": _now_iso(),
        }
    ).execute()

    sb.table("wallet_balances_cache").upsert(
        {
            "user_id": intent["user_id"],
            "kwami_id": intent["kwami_id"],
            "wallet_id": intent["wallet_id"],
            "mint_address": intent["asset_mint"],
            "symbol": intent["asset_symbol"],
            "amount": str(amount_received),
            "amount_usd": str(amount_usd_dec) if amount_usd_dec is not None else None,
            "updated_at": _now_iso(),
        },
        on_conflict="wallet_id,mint_address",
    ).execute()

    sb.table("wallet_transactions").insert(
        {
            "user_id": intent["user_id"],
            "kwami_id": intent["kwami_id"],
            "wallet_id": intent["wallet_id"],
            "mint_address": intent["asset_mint"],
            "symbol": intent["asset_symbol"],
            "direction": "in",
            "amount": str(amount_received),
            "amount_usd": str(amount_usd_dec) if amount_usd_dec is not None else None,
            "transaction_signature": tx_sig,
            "related_intent_id": intent["id"],
            "metadata": {"provider": provider},
            "created_at": _now_iso(),
        }
    ).execute()

    credits = _compute_credit_amount(
        amount_received, amount_usd_dec, asset_symbol=intent.get("asset_symbol")
    )
    # 'purchase', not 'wallet_funding': the latter is not a member of
    # credit_transaction_type, so this call raised every single time. The source
    # is recorded in metadata, which is indexed (016_wallet_funding_repair.sql)
    # and is what `_already_credited` matches on.
    new_balance = await add_credits(
        user_id=intent["user_id"],
        amount_micro=credits,
        transaction_type="purchase",
        description=f"Wallet funding confirmed ({provider})",
        metadata={
            "source": "wallet",
            "intent_id": intent["id"],
            "kwami_id": intent["kwami_id"],
            "asset_symbol": intent["asset_symbol"],
            "asset_mint": intent["asset_mint"],
            "amount": str(amount_received),
            "amount_usd": str(amount_usd_dec) if amount_usd_dec is not None else None,
            "transaction_signature": tx_sig,
        },
    )

    # Confirmed LAST. If anything above fails the intent stays pending and the
    # provider's retry runs the whole flow again; `_already_credited` makes that
    # retry safe.
    sb.table("wallet_funding_intents").update(
        {
            "status": "confirmed",
            "provider_intent_id": payload.get("provider_intent_id"),
            "updated_at": _now_iso(),
        }
    ).eq("id", intent_id).execute()

    return {
        "status": "confirmed",
        "intent_id": intent_id,
        "credits_added_micro": credits,
        "new_balance_micro": new_balance,
    }
