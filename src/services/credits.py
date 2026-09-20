"""Credits ledger and settlement logic."""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
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
from supabase import AsyncClient, create_async_client

logger = logging.getLogger("kwami-api.credits")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MICRO_CREDITS_PER_CREDIT = 1000
USD_PER_CREDIT = 0.001  # 1 credit = $0.001

# How much precision survives before `usd_to_micro_credits` rounds up: six decimal
# places of a micro-credit, i.e. 1e-12 USD. Fine enough that no real fraction is lost
# -- one token of the cheapest model is ~0.15 micro-credits -- and coarse enough to
# erase the float noise the pricing arithmetic leaves behind, which reaches ~1e-9 of a
# micro-credit on the largest charges. See that function for why rounding up without
# this over-bills.
QUANTIZE_EXPONENT = Decimal("1e-6")
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

_supabase_client: AsyncClient | None = None


async def init_supabase_admin() -> AsyncClient:
    """Build the one shared async client. Called from the application lifespan.

    Construction is a coroutine (`create_async_client` opens the underlying httpx
    session), which is why it cannot happen lazily inside `get_supabase_admin`.
    Building it once at startup is also what gives every request a shared
    connection pool instead of a fresh socket.
    """
    global _supabase_client
    if _supabase_client is None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY must be set for the credits system"
            )
        _supabase_client = await create_async_client(
            settings.supabase_url,
            settings.supabase_secret_key,
        )
    return _supabase_client


def get_supabase_admin() -> AsyncClient:
    """The shared admin client.

    Deliberately *not* a coroutine. On the async client `.table()`, `.select()` and
    the rest of the builder are ordinary synchronous calls; only `.execute()` is
    awaited. Keeping this synchronous means the 66 call sites stay
    `sb = get_supabase_admin()` and only the terminal `await ... .execute()` changes.
    """
    if _supabase_client is None:
        raise RuntimeError(
            "Supabase client is not initialised. The application lifespan calls "
            "init_supabase_admin(); a script or test reaching the database must too."
        )
    return _supabase_client


# ---------------------------------------------------------------------------
# Cost to credits conversion
# ---------------------------------------------------------------------------


def usd_to_micro_credits(cost_usd: float) -> int:
    """Convert a billed USD amount to micro-credits, rounding up.

    The ledger is integer micro-credits, so this is the one place a real-valued
    cost becomes money. It has to round the customer's way -- up -- or the
    platform absorbs the remainder on every usage item.

    ``int()`` truncated. The docstring has always said "rounded up"; the code
    floored. Measured across 200k realistic usage items, 57% were a micro-credit
    short.

    Rounding up is not on its own the fix, because ``cost_usd`` arrives carrying
    float noise from the pricing arithmetic upstream: ``(9128 / 1e6) * 15.0 * 2.0``
    is ``0.27384000000000003``, not ``0.27384``. Rounding *that* up bills 273841
    for a charge that is exactly 273840 micro-credits -- the same defect turned
    against the customer, on about 6% of items. Naive ``math.ceil`` and a naive
    ``Decimal(str(cost_usd))`` both do this.

    So the noise is quantized away first, on the micro-credit scale where it lands
    rather than on the USD scale where it started -- multiplying by a million
    multiplies the absolute error too, which is why quantizing the USD figure does
    not work. ``QUANTIZE_EXPONENT`` keeps six decimal places of a micro-credit: finer
    than any real fraction (one token of the cheapest model is ~0.15 micro-credits),
    coarser than the noise. What survives is the amount the pricing tables meant, and
    ``ROUND_CEILING`` then rounds the customer's way exactly once.

    Args:
        cost_usd: Customer-facing billed cost in USD.

    Returns:
        Amount in micro-credits, rounded up, never below 1.
    """
    micro_per_usd = Decimal(MICRO_CREDITS_PER_CREDIT) / Decimal(str(USD_PER_CREDIT))
    exact = Decimal(str(cost_usd)) * micro_per_usd
    denoised = exact.quantize(QUANTIZE_EXPONENT, rounding=ROUND_HALF_EVEN)
    micro = denoised.to_integral_value(rounding=ROUND_CEILING)
    return max(int(micro), 1)  # minimum 1 micro-credit per operation


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
    result = await sb.table("user_credits").select("*").eq("user_id", user_id).execute()

    if result.data:
        row = result.data[0]
        return {
            "balance": row["balance"],
            "lifetime_purchased": row["lifetime_purchased"],
            "lifetime_used": row["lifetime_used"],
            "updated_at": row["updated_at"],
        }

    # User has no row yet (shouldn't happen with trigger, but handle gracefully)
    await (
        sb.table("user_credits")
        .insert(
            {
                "user_id": user_id,
                "balance": 0,
                "lifetime_purchased": 0,
                "lifetime_used": 0,
            }
        )
        .execute()
    )

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
    idempotency_key: str | None = None,
) -> int:
    """Add credits to a user's balance using the DB function.

    Passing ``idempotency_key`` makes the call safe to retry: the ledger insert
    claims the key inside the same transaction as the balance update, so a repeat
    is a no-op that returns the current balance. Without it, a redelivered
    webhook credits the user again.

    Returns the new balance in micro-credits.
    """
    sb = get_supabase_admin()
    result = await sb.rpc(
        "add_credits",
        {
            "p_user_id": user_id,
            "p_amount": amount_micro,
            "p_type": transaction_type,
            "p_description": description or "",
            "p_metadata": metadata or {},
            "p_idempotency_key": idempotency_key,
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
        result = await sb.rpc(
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


async def resolve_ledger_user_id(reported_id: str) -> str:
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
    kwami_hit = await sb.table("user_kwamis").select("user_id").eq("id", rid).limit(1).execute()
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
        await sb.table("credit_usage_logs")
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
    await (
        sb.table("credit_usage_logs")
        .update(
            {
                "credits_charged": credits_charged,
                "settlement_status": settlement_status,
            }
        )
        .eq("id", usage_log_id)
        .execute()
    )


async def get_transactions(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Get paginated transaction history for a user."""
    sb = get_supabase_admin()
    result = (
        await sb.table("credit_transactions")
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

    result = await query.range(offset, offset + limit - 1).execute()
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

    result = await query.limit(limit).execute()
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


def build_report_key(user_id: str, session_id: str, usage_items: list[dict]) -> str:
    """Stable key for one usage report.

    Derived from the content so an agent that retries without supplying an
    explicit key still cannot be charged twice. Canonical JSON, so key order in
    the payload does not change the digest.
    """
    canonical = json.dumps(
        {"user_id": user_id, "session_id": session_id, "items": usage_items},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _find_usage_report(report_key: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    result = (
        await sb.table("usage_reports")
        .select("id, report_key, status, result")
        .eq("report_key", report_key)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


async def process_usage_report(
    user_id: str,
    session_id: str,
    usage_items: list[dict],
    idempotency_key: str | None = None,
) -> dict:
    """Process a batch usage report from the agent.

    Each item in usage_items: {model_type, model_id, units_used}

    Replay-safe: the report is claimed under a key before anything is charged, so
    a redelivered report returns the original result rather than debiting the
    user a second time.

    Returns summary with total_credits_charged and new_balance.
    """
    ledger_user_id = await resolve_ledger_user_id(user_id)
    if ledger_user_id != user_id:
        logger.info(
            "Usage report user id mapped kwami or alias -> ledger user: %s -> %s",
            user_id,
            ledger_user_id,
        )

    report_key = idempotency_key or build_report_key(user_id, session_id, usage_items)
    existing = await _find_usage_report(report_key)
    if existing is not None:
        # Return what the original call returned. Recomputing would settle against
        # a balance that has since moved, and would charge again.
        logger.info("Usage report %s already settled; returning the cached result", report_key)
        cached = dict(existing.get("result") or {})
        cached["idempotent_replay"] = True
        return cached

    sb = get_supabase_admin()
    try:
        await (
            sb.table("usage_reports")
            .insert(
                {
                    "report_key": report_key,
                    "user_id": ledger_user_id,
                    "session_id": session_id,
                    "status": "pending",
                    "items_count": len(usage_items),
                }
            )
            .execute()
        )
    except Exception as exc:
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            # Lost a race with a concurrent delivery of the same report.
            concurrent = await _find_usage_report(report_key)
            cached = dict((concurrent or {}).get("result") or {})
            cached["idempotent_replay"] = True
            return cached
        raise

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

    # Settle. Charge what the user can actually pay rather than all-or-nothing:
    # the previous behaviour charged ZERO whenever the total exceeded the balance,
    # so anyone running a low balance got an unbounded free session. The
    # shortfall is recorded instead of forgiven.
    new_balance = 0
    settlement_status = "skipped"
    unpaid_micro_credits = 0

    if total_requested_micro_credits > 0:
        balance_before = (await get_balance(ledger_user_id))["balance"]
        charged = min(total_requested_micro_credits, max(balance_before, 0))
        unpaid_micro_credits = total_requested_micro_credits - charged

        if charged > 0:
            try:
                new_balance = await deduct_credits(
                    user_id=ledger_user_id,
                    amount_micro=charged,
                    description=f"Session usage: {session_id}",
                    metadata={
                        "session_id": session_id,
                        "items_count": len(logged_items),
                        "requested_micro": total_requested_micro_credits,
                        "unpaid_micro": unpaid_micro_credits,
                    },
                )
                total_charged_micro_credits = charged
                settlement_status = "charged" if not unpaid_micro_credits else "partially_charged"
            except ValueError:
                # Balance moved between the read and the deduct.
                settlement_status = "insufficient_credits"
                unpaid_micro_credits = total_requested_micro_credits
                new_balance = balance_before
        else:
            settlement_status = "insufficient_credits"
            new_balance = balance_before

        if unpaid_micro_credits:
            logger.warning(
                "Session %s for user %s was under-funded: requested %d, charged %d, "
                "unpaid %d micro-credits",
                session_id,
                ledger_user_id,
                total_requested_micro_credits,
                total_charged_micro_credits,
                unpaid_micro_credits,
            )

    # Allocate what was actually charged across the items, in order, so the
    # per-row settlement adds up to the ledger entry.
    remaining = total_charged_micro_credits
    for logged_item in logged_items:
        requested = logged_item["requested_credits"]
        if settlement_status in ("charged", "partially_charged") and remaining > 0:
            credits_charged = min(requested, remaining)
            remaining -= credits_charged
            item_status = "charged" if credits_charged == requested else "partially_charged"
        else:
            credits_charged = 0
            item_status = "written_off" if settlement_status != "skipped" else settlement_status

        await update_usage_settlement(
            logged_item["usage_log_id"],
            credits_charged=credits_charged,
            settlement_status=item_status,
        )
        logged_item["credits_charged"] = credits_charged
        logged_item["settlement_status"] = item_status

    result = {
        "total_credits_requested": total_requested_micro_credits,
        "total_credits_charged": total_charged_micro_credits,
        "new_balance": new_balance,
        "total_provider_cost_usd": _round_usd(total_provider_cost_usd),
        "total_billed_cost_usd": _round_usd(total_billed_cost_usd),
        "total_margin_usd": _round_usd(total_margin_usd),
        "settlement_status": settlement_status,
        "unpaid_credits": unpaid_micro_credits,
        "items": logged_items,
    }

    # Cache the outcome so a replay returns this answer instead of re-settling.
    await _finalize_usage_report(
        report_key,
        status="settled" if not unpaid_micro_credits else "partially_settled",
        requested_micro=total_requested_micro_credits,
        charged_micro=total_charged_micro_credits,
        unpaid_micro=unpaid_micro_credits,
        result=result,
    )
    return result


async def _finalize_usage_report(
    report_key: str,
    *,
    status: str,
    requested_micro: int,
    charged_micro: int,
    unpaid_micro: int,
    result: dict[str, Any],
) -> None:
    """Record the settled report. Best effort: never mask a completed settlement."""
    sb = get_supabase_admin()
    try:
        await (
            sb.table("usage_reports")
            .update(
                {
                    "status": status,
                    "requested_micro": requested_micro,
                    "charged_micro": charged_micro,
                    "unpaid_micro": unpaid_micro,
                    "result": result,
                    "settled_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("report_key", report_key)
            .execute()
        )
    except Exception:
        logger.exception("Could not record the outcome of usage report %s", report_key)
