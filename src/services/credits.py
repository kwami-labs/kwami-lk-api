"""Credits ledger and settlement logic."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core.config import settings
from src.services.pricing import (
    ALL_PRICING,
    PRICING_VERSION,
    AudioPricing,
    ExternalPricing,
    RealtimePricing,
    TokenPricing,
    calculate_audio_cost,
    calculate_external_cost,
    calculate_realtime_cost,
    calculate_token_cost,
)
from supabase import Client, create_client

logger = logging.getLogger("kwami-api.credits")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MICRO_CREDITS_PER_CREDIT = 1000
USD_PER_CREDIT = 0.001  # 1 credit = $0.001
MARKUP_MULTIPLIER = settings.billing_markup_multiplier
FIXED_FEE_USD = settings.billing_fixed_fee_usd

# Credit pack definitions: (pack_id, display_name, credits, price_cents)
CREDIT_PACKS = {
    "starter": {
        "id": "starter",
        "name": "Spark",
        "credits": 5_000,
        "price_cents": 500,  # $5.00
        "popular": False,
    },
    "standard": {
        "id": "standard",
        "name": "Surge",
        "credits": 25_000,
        "price_cents": 2500,  # $25.00
        "popular": True,
    },
    "pro": {
        "id": "pro",
        "name": "Overcharge",
        "credits": 100_000,
        "price_cents": 10000,  # $100.00
        "popular": False,
    },
}

# ---------------------------------------------------------------------------
# Supabase admin client (singleton)
# ---------------------------------------------------------------------------

_supabase_client: Client | None = None


def get_supabase_admin() -> Client:
    """Get the Supabase admin client using the secret API key."""
    global _supabase_client
    if _supabase_client is None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY must be set for the credits system"
            )
        _supabase_client = create_client(
            settings.supabase_url,
            settings.supabase_secret_key,
        )
    return _supabase_client


# ---------------------------------------------------------------------------
# Cost to credits conversion
# ---------------------------------------------------------------------------


def usd_to_micro_credits(cost_usd: float) -> int:
    """Convert a billed USD amount to micro-credits.

    Args:
        cost_usd: Customer-facing billed cost in USD.

    Returns:
        Amount in micro-credits (rounded up).
    """
    credits = cost_usd / USD_PER_CREDIT
    micro = int(credits * MICRO_CREDITS_PER_CREDIT)
    return max(micro, 1)  # minimum 1 micro-credit per operation


@dataclass(slots=True)
class PricingBreakdown:
    """Raw provider cost and customer charge for one usage item."""

    normalized_units_used: float
    provider_cost_usd: float
    billed_cost_usd: float
    margin_usd: float
    requested_micro_credits: int
    pricing_source: str
    usage_metadata: dict[str, Any]


def _round_usd(value: float) -> float:
    return round(value, 6)


def _extract_usage_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Preserve extra usage dimensions for future reconciliation."""
    excluded = {"model_type", "model_id", "units_used"}
    return {key: value for key, value in item.items() if key not in excluded and value is not None}


def _calculate_fallback_cost(model_type: str, units_used: float) -> float:
    """Fallback when the model or service is unknown."""
    if model_type == "llm":
        return (units_used / 1_000_000) * settings.billing_fallback_cost_per_1m_tokens_usd
    return 0.0


def _apply_billing_policy(provider_cost_usd: float) -> tuple[float, float, int]:
    """Apply platform markup and convert the result to micro-credits."""
    if provider_cost_usd <= 0:
        return 0.0, 0.0, 0
    billed_cost_usd = (provider_cost_usd * MARKUP_MULTIPLIER) + FIXED_FEE_USD
    margin_usd = billed_cost_usd - provider_cost_usd
    return billed_cost_usd, margin_usd, usd_to_micro_credits(billed_cost_usd)


def calculate_usage_charge(item: dict[str, Any]) -> PricingBreakdown:
    """Calculate raw provider cost and customer charge for one usage event."""
    model_id = item["model_id"]
    model_type = item["model_type"]
    units_used = float(item.get("units_used") or 0.0)
    usage_metadata = _extract_usage_metadata(item)

    pricing_entry = ALL_PRICING.get(model_id)
    pricing_source = f"catalog:{PRICING_VERSION}"
    provider_cost_usd = 0.0

    if pricing_entry is None:
        logger.warning("No pricing for model %s, using fallback", model_id)
        provider_cost_usd = _calculate_fallback_cost(model_type, units_used)
        pricing_source = "fallback"
        usage_metadata.setdefault("fallback_reason", "unknown_model")
    else:
        pricing = pricing_entry.pricing
        if isinstance(pricing, TokenPricing):
            prompt_tokens = int(item.get("prompt_tokens") or 0)
            completion_tokens = int(item.get("completion_tokens") or 0)
            cached_input_tokens = int(
                item.get("cached_input_tokens") or item.get("cached_tokens") or 0
            )
            if prompt_tokens or completion_tokens or cached_input_tokens:
                provider_cost_usd = calculate_token_cost(
                    pricing,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_input_tokens=cached_input_tokens,
                )
                units_used = float(item.get("units_used") or prompt_tokens + completion_tokens)
            else:
                average_price_per_1m = (pricing.input_per_1m + pricing.output_per_1m) / 2
                provider_cost_usd = (units_used / 1_000_000) * average_price_per_1m
                pricing_source = "estimated_total_tokens"
        elif isinstance(pricing, AudioPricing):
            provider_cost_usd = calculate_audio_cost(pricing, units_used)
        elif isinstance(pricing, RealtimePricing):
            provider_cost_usd = calculate_realtime_cost(
                pricing,
                audio_input_minutes=float(item.get("audio_input_minutes") or 0.0),
                audio_output_minutes=float(item.get("audio_output_minutes") or 0.0),
                text_input_tokens=int(item.get("text_input_tokens") or 0),
                text_output_tokens=int(item.get("text_output_tokens") or 0),
                fallback_minutes=units_used,
            )
            if (
                item.get("audio_input_minutes")
                or item.get("audio_output_minutes")
                or item.get("text_input_tokens")
                or item.get("text_output_tokens")
            ):
                pricing_source = "catalog:realtime_detailed"
        elif isinstance(pricing, ExternalPricing):
            if units_used <= 0:
                units_used = float(item.get("request_count") or 1.0)
            provider_cost_usd = calculate_external_cost(pricing, units_used)

    billed_cost_usd, margin_usd, micro_credits = _apply_billing_policy(provider_cost_usd)
    usage_metadata["units_used"] = round(units_used, 6)

    return PricingBreakdown(
        normalized_units_used=round(units_used, 6),
        provider_cost_usd=_round_usd(provider_cost_usd),
        billed_cost_usd=_round_usd(billed_cost_usd),
        margin_usd=_round_usd(margin_usd),
        requested_micro_credits=micro_credits,
        pricing_source=pricing_source,
        usage_metadata=usage_metadata,
    )


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


async def get_balance(user_id: str) -> dict[str, Any]:
    """Get a user's credit balance.

    Returns dict with balance, lifetime_purchased, lifetime_used (all in micro-credits).
    Creates a row with 0 balance if the user has no record.
    """
    sb = get_supabase_admin()
    result = sb.table("user_credits").select("*").eq("user_id", user_id).execute()

    if result.data:
        row = result.data[0]
        return {
            "balance": row["balance"],
            "lifetime_purchased": row["lifetime_purchased"],
            "lifetime_used": row["lifetime_used"],
            "updated_at": row["updated_at"],
        }

    # User has no row yet (shouldn't happen with trigger, but handle gracefully)
    sb.table("user_credits").insert(
        {
            "user_id": user_id,
            "balance": 0,
            "lifetime_purchased": 0,
            "lifetime_used": 0,
        }
    ).execute()

    return {
        "balance": 0,
        "lifetime_purchased": 0,
        "lifetime_used": 0,
        "updated_at": None,
    }


async def add_credits(
    user_id: str,
    amount_micro: int,
    transaction_type: str = "purchase",
    description: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Add credits to a user's balance using the DB function.

    Returns the new balance in micro-credits.
    """
    sb = get_supabase_admin()
    result = sb.rpc(
        "add_credits",
        {
            "p_user_id": user_id,
            "p_amount": amount_micro,
            "p_type": transaction_type,
            "p_description": description or "",
            "p_metadata": metadata or {},
        },
    ).execute()

    new_balance = result.data
    logger.info(f"Added {amount_micro} micro-credits to user {user_id}, new balance: {new_balance}")
    return new_balance


async def deduct_credits(
    user_id: str,
    amount_micro: int,
    description: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Deduct credits from a user's balance using the DB function.

    Returns the new balance. Raises if insufficient funds.
    """
    sb = get_supabase_admin()
    try:
        result = sb.rpc(
            "deduct_credits",
            {
                "p_user_id": user_id,
                "p_amount": amount_micro,
                "p_description": description or "",
                "p_metadata": metadata or {},
            },
        ).execute()

        new_balance = result.data
        logger.info(
            f"Deducted {amount_micro} micro-credits from user {user_id}, new balance: {new_balance}"
        )
        return new_balance
    except Exception as e:
        if "Insufficient credits" in str(e):
            raise ValueError("Insufficient credits") from e
        raise


def resolve_ledger_user_id(reported_id: str) -> str:
    """Map agent-reported id to Supabase `users.id` for `credit_usage_logs`.

    The agent often sends `kwami_id` (from telephony metadata) instead of the auth user id.
    """
    rid = str(reported_id).strip()
    if not rid:
        raise ValueError("user_id is required")

    try:
        uuid.UUID(rid)
    except ValueError:
        return rid

    sb = get_supabase_admin()
    kwami_hit = sb.table("user_kwamis").select("user_id").eq("id", rid).limit(1).execute()
    rows = getattr(kwami_hit, "data", None) or []
    if rows and rows[0].get("user_id"):
        return str(rows[0]["user_id"])
    return rid


async def log_usage(
    user_id: str,
    session_id: str,
    model_type: str,
    model_id: str,
    units_used: float,
    provider_cost_usd: float,
    billed_cost_usd: float,
    margin_usd: float,
    requested_credits: int,
    pricing_source: str,
    usage_metadata: dict[str, Any] | None = None,
) -> str:
    """Insert a pending usage log row and return its ID."""
    sb = get_supabase_admin()
    result = (
        sb.table("credit_usage_logs")
        .insert(
            {
                "user_id": user_id,
                "session_id": session_id,
                "model_type": model_type,
                "model_id": model_id,
                "units_used": units_used,
                "cost_usd": provider_cost_usd,
                "provider_cost_usd": provider_cost_usd,
                "billed_cost_usd": billed_cost_usd,
                "margin_usd": margin_usd,
                "requested_credits": requested_credits,
                "credits_charged": 0,
                "settlement_status": "pending",
                "pricing_version": PRICING_VERSION,
                "pricing_source": pricing_source,
                "usage_metadata": usage_metadata or {},
            }
        )
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    raise RuntimeError("Failed to insert credit usage log")


async def update_usage_settlement(
    usage_log_id: str,
    *,
    credits_charged: int,
    settlement_status: str,
) -> None:
    """Update a usage log with the final settlement result."""
    sb = get_supabase_admin()
    sb.table("credit_usage_logs").update(
        {
            "credits_charged": credits_charged,
            "settlement_status": settlement_status,
        }
    ).eq("id", usage_log_id).execute()


async def get_transactions(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Get paginated transaction history for a user."""
    sb = get_supabase_admin()
    result = (
        sb.table("credit_transactions")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return result.data or []


async def get_usage_logs(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
    session_id: str | None = None,
) -> list[dict]:
    """Get paginated usage logs for a user."""
    sb = get_supabase_admin()
    query = (
        sb.table("credit_usage_logs")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
    )

    if session_id:
        query = query.eq("session_id", session_id)

    result = query.range(offset, offset + limit - 1).execute()
    return result.data or []


async def get_usage_logs_for_reconciliation(
    user_id: str,
    *,
    limit: int = 500,
    session_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> list[dict]:
    """Fetch usage logs for reconciliation and margin analysis."""
    sb = get_supabase_admin()
    query = (
        sb.table("credit_usage_logs")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
    )

    if session_id:
        query = query.eq("session_id", session_id)
    if created_after:
        query = query.gte("created_at", created_after.isoformat())
    if created_before:
        query = query.lte("created_at", created_before.isoformat())

    result = query.limit(limit).execute()
    return result.data or []


def _infer_provider(model_id: str) -> str:
    pricing_entry = ALL_PRICING.get(model_id)
    if pricing_entry:
        return pricing_entry.provider
    if "/" in model_id:
        return model_id.split("/", 1)[0]
    return "unknown"


def build_reconciliation_report(logs: list[dict]) -> dict[str, Any]:
    """Build margin and anomaly reporting from usage log rows."""
    summary = {
        "usage_rows": len(logs),
        "sessions_count": len({log.get("session_id") for log in logs if log.get("session_id")}),
        "total_provider_cost_usd": 0.0,
        "total_billed_cost_usd": 0.0,
        "total_margin_usd": 0.0,
        "total_requested_credits": 0,
        "total_charged_credits": 0,
        "charged_rows": 0,
        "pending_rows": 0,
        "insufficient_rows": 0,
        "fallback_rows": 0,
    }
    provider_breakdown: dict[str, dict[str, Any]] = {}
    session_breakdown: dict[str, dict[str, Any]] = {}
    anomaly_counts = {
        "pending_settlement": 0,
        "insufficient_credits": 0,
        "fallback_pricing": 0,
        "zero_provider_cost": 0,
        "charged_without_revenue": 0,
    }

    for log in logs:
        provider_cost = float(log.get("provider_cost_usd") or log.get("cost_usd") or 0.0)
        billed_cost = float(log.get("billed_cost_usd") or provider_cost)
        margin = float(log.get("margin_usd") or (billed_cost - provider_cost))
        requested_credits = int(log.get("requested_credits") or log.get("credits_charged") or 0)
        charged_credits = int(log.get("credits_charged") or 0)
        settlement_status = log.get("settlement_status") or "charged"
        pricing_source = log.get("pricing_source") or "legacy"
        session_id = log.get("session_id") or "unknown"
        provider = _infer_provider(log.get("model_id") or "unknown")

        summary["total_provider_cost_usd"] += provider_cost
        summary["total_billed_cost_usd"] += billed_cost
        summary["total_margin_usd"] += margin
        summary["total_requested_credits"] += requested_credits
        summary["total_charged_credits"] += charged_credits

        if settlement_status == "charged":
            summary["charged_rows"] += 1
        elif settlement_status == "pending":
            summary["pending_rows"] += 1
            anomaly_counts["pending_settlement"] += 1
        elif settlement_status == "insufficient_credits":
            summary["insufficient_rows"] += 1
            anomaly_counts["insufficient_credits"] += 1

        if pricing_source == "fallback":
            summary["fallback_rows"] += 1
            anomaly_counts["fallback_pricing"] += 1
        if provider_cost <= 0:
            anomaly_counts["zero_provider_cost"] += 1
        if charged_credits > 0 and billed_cost <= 0:
            anomaly_counts["charged_without_revenue"] += 1

        provider_row = provider_breakdown.setdefault(
            provider,
            {
                "provider": provider,
                "usage_rows": 0,
                "sessions_count": set(),
                "provider_cost_usd": 0.0,
                "billed_cost_usd": 0.0,
                "margin_usd": 0.0,
                "requested_credits": 0,
                "charged_credits": 0,
            },
        )
        provider_row["usage_rows"] += 1
        provider_row["sessions_count"].add(session_id)
        provider_row["provider_cost_usd"] += provider_cost
        provider_row["billed_cost_usd"] += billed_cost
        provider_row["margin_usd"] += margin
        provider_row["requested_credits"] += requested_credits
        provider_row["charged_credits"] += charged_credits

        session_row = session_breakdown.setdefault(
            session_id,
            {
                "session_id": session_id,
                "usage_rows": 0,
                "providers": set(),
                "provider_cost_usd": 0.0,
                "billed_cost_usd": 0.0,
                "margin_usd": 0.0,
                "requested_credits": 0,
                "charged_credits": 0,
                "settlement_statuses": set(),
            },
        )
        session_row["usage_rows"] += 1
        session_row["providers"].add(provider)
        session_row["provider_cost_usd"] += provider_cost
        session_row["billed_cost_usd"] += billed_cost
        session_row["margin_usd"] += margin
        session_row["requested_credits"] += requested_credits
        session_row["charged_credits"] += charged_credits
        session_row["settlement_statuses"].add(settlement_status)

    for numeric_key in (
        "total_provider_cost_usd",
        "total_billed_cost_usd",
        "total_margin_usd",
    ):
        summary[numeric_key] = _round_usd(summary[numeric_key])
    summary["effective_margin_percent"] = round(
        (summary["total_margin_usd"] / summary["total_billed_cost_usd"] * 100)
        if summary["total_billed_cost_usd"] > 0
        else 0.0,
        2,
    )

    provider_items = []
    for row in provider_breakdown.values():
        provider_items.append(
            {
                "provider": row["provider"],
                "usage_rows": row["usage_rows"],
                "sessions_count": len(row["sessions_count"]),
                "provider_cost_usd": _round_usd(row["provider_cost_usd"]),
                "billed_cost_usd": _round_usd(row["billed_cost_usd"]),
                "margin_usd": _round_usd(row["margin_usd"]),
                "margin_percent": round(
                    (row["margin_usd"] / row["billed_cost_usd"] * 100)
                    if row["billed_cost_usd"] > 0
                    else 0.0,
                    2,
                ),
                "requested_credits": row["requested_credits"],
                "charged_credits": row["charged_credits"],
            }
        )
    provider_items.sort(key=lambda item: item["provider_cost_usd"], reverse=True)

    session_items = []
    for row in session_breakdown.values():
        settlement_states = row["settlement_statuses"]
        if len(settlement_states) == 1:
            settlement_status = next(iter(settlement_states))
        elif "insufficient_credits" in settlement_states:
            settlement_status = "insufficient_credits"
        elif "pending" in settlement_states:
            settlement_status = "pending"
        else:
            settlement_status = "mixed"
        session_items.append(
            {
                "session_id": row["session_id"],
                "usage_rows": row["usage_rows"],
                "providers": sorted(row["providers"]),
                "provider_cost_usd": _round_usd(row["provider_cost_usd"]),
                "billed_cost_usd": _round_usd(row["billed_cost_usd"]),
                "margin_usd": _round_usd(row["margin_usd"]),
                "requested_credits": row["requested_credits"],
                "charged_credits": row["charged_credits"],
                "settlement_status": settlement_status,
            }
        )
    session_items.sort(key=lambda item: item["provider_cost_usd"], reverse=True)

    anomalies = [
        {
            "type": anomaly_type,
            "count": count,
        }
        for anomaly_type, count in anomaly_counts.items()
        if count > 0
    ]
    anomalies.sort(key=lambda item: item["count"], reverse=True)

    return {
        "summary": summary,
        "provider_breakdown": provider_items,
        "session_breakdown": session_items,
        "anomalies": anomalies,
    }


async def get_reconciliation_report(
    user_id: str,
    *,
    limit: int = 500,
    session_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> dict[str, Any]:
    """Build a reconciliation-ready report for one user's ledger."""
    logs = await get_usage_logs_for_reconciliation(
        user_id,
        limit=limit,
        session_id=session_id,
        created_after=created_after,
        created_before=created_before,
    )
    report = build_reconciliation_report(logs)
    report["log_rows_scanned"] = len(logs)
    return report


async def process_usage_report(
    user_id: str,
    session_id: str,
    usage_items: list[dict],
) -> dict:
    """Process a batch usage report from the agent.

    Each item in usage_items: {model_type, model_id, units_used}

    Returns summary with total_credits_charged and new_balance.
    """
    ledger_user_id = resolve_ledger_user_id(user_id)
    if ledger_user_id != user_id:
        logger.info(
            "Usage report user id mapped kwami or alias -> ledger user: %s -> %s",
            user_id,
            ledger_user_id,
        )

    total_requested_micro_credits = 0
    total_charged_micro_credits = 0
    total_provider_cost_usd = 0.0
    total_billed_cost_usd = 0.0
    total_margin_usd = 0.0
    logged_items = []

    for item in usage_items:
        model_type = item["model_type"]
        model_id = item["model_id"]

        breakdown = calculate_usage_charge(item)
        usage_log_id = await log_usage(
            user_id=ledger_user_id,
            session_id=session_id,
            model_type=model_type,
            model_id=model_id,
            units_used=breakdown.normalized_units_used,
            provider_cost_usd=breakdown.provider_cost_usd,
            billed_cost_usd=breakdown.billed_cost_usd,
            margin_usd=breakdown.margin_usd,
            requested_credits=breakdown.requested_micro_credits,
            pricing_source=breakdown.pricing_source,
            usage_metadata=breakdown.usage_metadata,
        )

        total_requested_micro_credits += breakdown.requested_micro_credits
        total_provider_cost_usd += breakdown.provider_cost_usd
        total_billed_cost_usd += breakdown.billed_cost_usd
        total_margin_usd += breakdown.margin_usd
        logged_items.append(
            {
                "usage_log_id": usage_log_id,
                "model_type": model_type,
                "model_id": model_id,
                "units_used": breakdown.normalized_units_used,
                "provider_cost_usd": breakdown.provider_cost_usd,
                "billed_cost_usd": breakdown.billed_cost_usd,
                "margin_usd": breakdown.margin_usd,
                "requested_credits": breakdown.requested_micro_credits,
                "credits_charged": 0,
                "pricing_source": breakdown.pricing_source,
                "settlement_status": "pending",
            }
        )

    # Deduct total from user balance
    new_balance = 0
    settlement_status = "skipped"
    if total_requested_micro_credits > 0:
        try:
            new_balance = await deduct_credits(
                user_id=ledger_user_id,
                amount_micro=total_requested_micro_credits,
                description=f"Session usage: {session_id}",
                metadata={
                    "session_id": session_id,
                    "items_count": len(logged_items),
                },
            )
            settlement_status = "charged"
            total_charged_micro_credits = total_requested_micro_credits
        except ValueError:
            settlement_status = "insufficient_credits"
            logger.warning(
                f"Insufficient credits for user {ledger_user_id}, "
                f"session {session_id}. Usage logged but not fully charged."
            )

    for logged_item in logged_items:
        credits_charged = logged_item["requested_credits"] if settlement_status == "charged" else 0
        await update_usage_settlement(
            logged_item["usage_log_id"],
            credits_charged=credits_charged,
            settlement_status=settlement_status,
        )
        logged_item["credits_charged"] = credits_charged
        logged_item["settlement_status"] = settlement_status

    return {
        "total_credits_requested": total_requested_micro_credits,
        "total_credits_charged": total_charged_micro_credits,
        "new_balance": new_balance,
        "total_provider_cost_usd": _round_usd(total_provider_cost_usd),
        "total_billed_cost_usd": _round_usd(total_billed_cost_usd),
        "total_margin_usd": _round_usd(total_margin_usd),
        "settlement_status": settlement_status,
        "items": logged_items,
    }
