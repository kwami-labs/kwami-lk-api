"""Credits system endpoints.

Provides endpoints for:
- Viewing credit balance and transaction history
- Purchasing credits via Stripe Checkout
- Stripe webhook for payment confirmation
- Agent usage reporting (Kwami API key auth)
"""

import hmac
import logging
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.config import settings
from src.core.security import AuthUser
from src.services.credits import (
    CREDIT_PACKS,
    MICRO_CREDITS_PER_CREDIT,
    get_balance,
    get_reconciliation_report,
    get_transactions,
    get_usage_logs,
    process_usage_report,
)
from src.services.stripe_service import create_checkout_session, handle_webhook_event

logger = logging.getLogger("kwami-api.credits")
router = APIRouter()


# =============================================================================
# Response Models
# =============================================================================


class CreditBalanceResponse(BaseModel):
    """User credit balance."""

    balance: int = Field(description="Current balance in micro-credits")
    balance_credits: float = Field(description="Current balance in display credits")
    lifetime_purchased: int = Field(description="Total purchased in micro-credits")
    lifetime_used: int = Field(description="Total used in micro-credits")


class CreditPackResponse(BaseModel):
    """A purchasable credit pack."""

    id: str
    name: str
    credits: int
    price_cents: int
    price_display: str
    popular: bool


class CreditPacksResponse(BaseModel):
    """All available credit packs."""

    packs: list[CreditPackResponse]


class PurchaseRequest(BaseModel):
    """Request to initiate a credit purchase."""

    pack_id: str = Field(..., description="Credit pack ID: starter, standard, or pro")
    success_url: str = Field(..., description="URL to redirect on successful payment")
    cancel_url: str = Field(..., description="URL to redirect on cancelled payment")


class PurchaseResponse(BaseModel):
    """Response with Stripe Checkout URL."""

    checkout_url: str


class TransactionItem(BaseModel):
    """A credit transaction record."""

    id: str
    type: str
    amount: int
    balance_after: int
    description: str | None
    metadata: dict | None
    created_at: str


class TransactionsResponse(BaseModel):
    """Paginated transaction history."""

    transactions: list[TransactionItem]
    count: int


class UsageLogItem(BaseModel):
    """A usage log record."""

    id: str
    session_id: str
    model_type: str
    model_id: str
    units_used: float
    cost_usd: float
    provider_cost_usd: float | None = None
    billed_cost_usd: float | None = None
    margin_usd: float | None = None
    requested_credits: int | None = None
    credits_charged: int
    settlement_status: str | None = None
    pricing_version: str | None = None
    pricing_source: str | None = None
    usage_metadata: dict | None = None
    created_at: str


class UsageLogsResponse(BaseModel):
    """Paginated usage logs."""

    logs: list[UsageLogItem]
    count: int


class UsageReportItem(BaseModel):
    """A single usage item in an agent report."""

    model_type: str = Field(..., description="stt, llm, tts, or realtime")
    model_id: str = Field(..., description="Model identifier")
    units_used: float = Field(..., description="Tokens, minutes, or characters")
    prompt_tokens: int | None = Field(None, description="Prompt/input tokens for LLM usage")
    completion_tokens: int | None = Field(None, description="Completion/output tokens for LLM usage")
    cached_input_tokens: int | None = Field(None, description="Cached input tokens if the provider supports them")
    audio_input_minutes: float | None = Field(None, description="Realtime audio input minutes when available")
    audio_output_minutes: float | None = Field(None, description="Realtime audio output minutes when available")
    text_input_tokens: int | None = Field(None, description="Realtime text input tokens when available")
    text_output_tokens: int | None = Field(None, description="Realtime text output tokens when available")
    request_count: int | None = Field(None, description="Request count for tool or memory operations")
    event_count: int | None = Field(None, description="How many events were aggregated into this item")


class UsageReportRequest(BaseModel):
    """Agent usage report request."""

    user_id: str = Field(..., description="Supabase user ID")
    session_id: str = Field(..., description="LiveKit room name")
    usage: list[UsageReportItem] = Field(..., description="Usage items")


class UsageReportResponse(BaseModel):
    """Response from processing a usage report."""

    total_credits_requested: int
    total_credits_charged: int
    new_balance: int
    total_provider_cost_usd: float
    total_billed_cost_usd: float
    total_margin_usd: float
    settlement_status: str
    items_processed: int


class ReconciliationSummary(BaseModel):
    usage_rows: int
    sessions_count: int
    total_provider_cost_usd: float
    total_billed_cost_usd: float
    total_margin_usd: float
    total_requested_credits: int
    total_charged_credits: int
    charged_rows: int
    pending_rows: int
    insufficient_rows: int
    fallback_rows: int
    effective_margin_percent: float


class ProviderReconciliationItem(BaseModel):
    provider: str
    usage_rows: int
    sessions_count: int
    provider_cost_usd: float
    billed_cost_usd: float
    margin_usd: float
    margin_percent: float
    requested_credits: int
    charged_credits: int


class SessionReconciliationItem(BaseModel):
    session_id: str
    usage_rows: int
    providers: list[str]
    provider_cost_usd: float
    billed_cost_usd: float
    margin_usd: float
    requested_credits: int
    charged_credits: int
    settlement_status: str


class ReconciliationAnomaly(BaseModel):
    type: str
    count: int


class ReconciliationResponse(BaseModel):
    summary: ReconciliationSummary
    provider_breakdown: list[ProviderReconciliationItem]
    session_breakdown: list[SessionReconciliationItem]
    anomalies: list[ReconciliationAnomaly]
    log_rows_scanned: int


# =============================================================================
# Endpoints - User-facing (require auth)
# =============================================================================


@router.get("/balance", response_model=CreditBalanceResponse)
async def get_credit_balance(
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """Get the current user's credit balance."""
    data = await get_balance(user.id)
    balance = data["balance"]
    return CreditBalanceResponse(
        balance=balance,
        balance_credits=balance / MICRO_CREDITS_PER_CREDIT,
        lifetime_purchased=data["lifetime_purchased"],
        lifetime_used=data["lifetime_used"],
    )


@router.get("/packs", response_model=CreditPacksResponse)
async def get_credit_packs():
    """Get available credit packs for purchase."""
    packs = []
    for pack in CREDIT_PACKS.values():
        packs.append(CreditPackResponse(
            id=pack["id"],
            name=pack["name"],
            credits=pack["credits"],
            price_cents=pack["price_cents"],
            price_display=f"${pack['price_cents'] / 100:.2f}",
            popular=pack["popular"],
        ))
    return CreditPacksResponse(packs=packs)


@router.post("/purchase", response_model=PurchaseResponse)
async def purchase_credits(
    request: PurchaseRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """Create a Stripe Checkout Session to purchase credits."""
    try:
        checkout_url = await create_checkout_session(
            user_id=user.id,
            pack_id=request.pack_id,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
        )
        return PurchaseResponse(checkout_url=checkout_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error(f"Stripe not configured: {e}")
        raise HTTPException(
            status_code=503,
            detail="Payment processing is not currently available",
        )


@router.get("/transactions", response_model=TransactionsResponse)
async def get_credit_transactions(
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Get the user's credit transaction history."""
    transactions = await get_transactions(user.id, limit=limit, offset=offset)
    items = [
        TransactionItem(
            id=t["id"],
            type=t["type"],
            amount=t["amount"],
            balance_after=t["balance_after"],
            description=t.get("description"),
            metadata=t.get("metadata"),
            created_at=t["created_at"],
        )
        for t in transactions
    ]
    return TransactionsResponse(transactions=items, count=len(items))


@router.get("/usage", response_model=UsageLogsResponse)
async def get_credit_usage(
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session_id: Optional[str] = Query(None),
):
    """Get the user's credit usage logs."""
    logs = await get_usage_logs(
        user.id,
        limit=limit,
        offset=offset,
        session_id=session_id,
    )
    items = [
        UsageLogItem(
            id=l["id"],
            session_id=l["session_id"],
            model_type=l["model_type"],
            model_id=l["model_id"],
            units_used=l["units_used"],
            cost_usd=l["cost_usd"],
            provider_cost_usd=l.get("provider_cost_usd"),
            billed_cost_usd=l.get("billed_cost_usd"),
            margin_usd=l.get("margin_usd"),
            requested_credits=l.get("requested_credits"),
            credits_charged=l["credits_charged"],
            settlement_status=l.get("settlement_status"),
            pricing_version=l.get("pricing_version"),
            pricing_source=l.get("pricing_source"),
            usage_metadata=l.get("usage_metadata"),
            created_at=l["created_at"],
        )
        for l in logs
    ]
    return UsageLogsResponse(logs=items, count=len(items))


@router.get("/reconciliation", response_model=ReconciliationResponse)
async def get_credit_reconciliation(
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: int = Query(500, ge=1, le=2000),
    session_id: Optional[str] = Query(None),
    created_after: Optional[datetime] = Query(None),
    created_before: Optional[datetime] = Query(None),
):
    """Get a reconciliation-ready ledger summary for the current user."""
    report = await get_reconciliation_report(
        user.id,
        limit=limit,
        session_id=session_id,
        created_after=created_after,
        created_before=created_before,
    )
    return ReconciliationResponse(**report)


# =============================================================================
# Endpoints - Stripe Webhook (no user auth, verified by Stripe signature)
# =============================================================================


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events.

    Verified using the Stripe-Signature header, not user auth.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        result = await handle_webhook_event(payload, sig_header)
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except RuntimeError as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Webhook processing failed")


# =============================================================================
# Endpoints - Agent Usage Report (Kwami API key auth)
# =============================================================================


def _verify_kwami_api_key(
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> None:
    """Verify the Kwami API key used by the agent to report usage."""
    if not settings.kwami_api_key or not settings.kwami_api_key.strip():
        raise HTTPException(
            status_code=503,
            detail="Kwami API key not configured (set KWAMI_API_KEY on the API server)",
        )
    # Constant-time: `!=` on a shared secret leaks length and prefix through timing.
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.kwami_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


@router.post("/usage/report", response_model=UsageReportResponse)
async def report_usage(
    request: UsageReportRequest,
    _: Annotated[None, Depends(_verify_kwami_api_key)],
):
    """Report AI usage from the agent after a session ends.

    This endpoint is called by the LiveKit agent (not the frontend).
    Authenticated via Kwami API key (X-API-Key header).
    """
    logger.info(
        f"Usage report received: user={request.user_id}, "
        f"session={request.session_id}, items={len(request.usage)}"
    )

    usage_items = [
        {
            "model_type": item.model_type,
            "model_id": item.model_id,
            "units_used": item.units_used,
            "prompt_tokens": item.prompt_tokens,
            "completion_tokens": item.completion_tokens,
            "cached_input_tokens": item.cached_input_tokens,
            "audio_input_minutes": item.audio_input_minutes,
            "audio_output_minutes": item.audio_output_minutes,
            "text_input_tokens": item.text_input_tokens,
            "text_output_tokens": item.text_output_tokens,
            "request_count": item.request_count,
            "event_count": item.event_count,
        }
        for item in request.usage
    ]

    result = await process_usage_report(
        user_id=request.user_id,
        session_id=request.session_id,
        usage_items=usage_items,
    )

    return UsageReportResponse(
        total_credits_requested=result["total_credits_requested"],
        total_credits_charged=result["total_credits_charged"],
        new_balance=result["new_balance"],
        total_provider_cost_usd=result["total_provider_cost_usd"],
        total_billed_cost_usd=result["total_billed_cost_usd"],
        total_margin_usd=result["total_margin_usd"],
        settlement_status=result["settlement_status"],
        items_processed=len(result["items"]),
    )
