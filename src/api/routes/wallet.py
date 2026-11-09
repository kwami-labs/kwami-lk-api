"""Wallet endpoints for kwami wallets."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.config import settings
from src.core.security import AuthUser
from src.services.custody_service import CustodyError
from src.services.wallet_service import (
    add_custom_allowlist_token,
    create_funding_intent,
    create_kwami_wallet,
    get_kwami_wallet_overview,
    settle_funding_intent,
    verify_wallet_webhook_signature,
)

router = APIRouter()


def _ensure_wallet_enabled() -> None:
    if not settings.wallet_enabled:
        raise HTTPException(status_code=503, detail="Wallet feature is disabled")


class WalletOverviewResponse(BaseModel):
    wallet: dict | None
    balances: list[dict]
    allowlist: list[dict]
    transactions: list[dict]
    funding_intents: list[dict] = Field(default_factory=list)


class FundingIntentRequest(BaseModel):
    asset_mint: str = Field(alias="assetMint")
    asset_symbol: str = Field(alias="assetSymbol")
    amount: Decimal
    amount_usd: Decimal | None = Field(default=None, alias="amountUsd")
    sender_wallet_pubkey: str | None = Field(default=None, alias="senderWalletPubkey")
    idempotency_key: str | None = Field(default=None, alias="idempotencyKey")


class AllowlistTokenRequest(BaseModel):
    mint_address: str = Field(alias="mintAddress")
    symbol: str
    decimals: int = Field(ge=0, le=18)
    is_stablecoin: bool = Field(default=False, alias="isStablecoin")


@router.post("/kwamis/{kwami_id}")
async def create_wallet_for_kwami(
    kwami_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """Create the custodial Solana wallet for a kwami, or return the existing one.

    Requires a user JWT and a wallet-enabled deployment (503 otherwise).
    Responds 404 if the kwami is not the caller's and 503 if the custody provider fails."""
    _ensure_wallet_enabled()
    try:
        wallet = await create_kwami_wallet(user.id, kwami_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CustodyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"wallet": wallet}


@router.get("/kwamis/{kwami_id}", response_model=WalletOverviewResponse)
async def get_wallet_for_kwami(
    kwami_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """Return a kwami's wallet with balances, token allowlist, transactions and funding intents.

    Requires a user JWT. Responds 503 if the wallet feature is disabled and 404 if the kwami is not found."""
    _ensure_wallet_enabled()
    try:
        overview = await get_kwami_wallet_overview(user.id, kwami_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return WalletOverviewResponse(**overview)


@router.post("/kwamis/{kwami_id}/fund/phantom-intent")
async def create_phantom_funding_intent(
    kwami_id: str,
    request: FundingIntentRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """Create a funding intent to be paid by a Phantom wallet transfer.

    Requires a user JWT. The body gives the token mint, symbol and amount, plus an optional idempotency key.
    Responds 400 if the amount is not positive, the mint is not allowlisted, or the kwami has no wallet."""
    _ensure_wallet_enabled()
    try:
        intent = await create_funding_intent(
            user.id,
            kwami_id=kwami_id,
            provider="phantom_transfer",
            asset_mint=request.asset_mint,
            asset_symbol=request.asset_symbol,
            amount=request.amount,
            amount_usd=request.amount_usd,
            sender_wallet_pubkey=request.sender_wallet_pubkey,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"intent": intent}


@router.post("/kwamis/{kwami_id}/fund/card-intent")
async def create_card_funding_intent(
    kwami_id: str,
    request: FundingIntentRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """Create a funding intent to be paid through the card provider.

    Requires a user JWT. Takes the same body as the Phantom intent and fails the same way (400)
    if the amount is not positive, the mint is not allowlisted, or the kwami has no wallet."""
    _ensure_wallet_enabled()
    try:
        intent = await create_funding_intent(
            user.id,
            kwami_id=kwami_id,
            provider="card_provider",
            asset_mint=request.asset_mint,
            asset_symbol=request.asset_symbol,
            amount=request.amount,
            amount_usd=request.amount_usd,
            sender_wallet_pubkey=request.sender_wallet_pubkey,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"intent": intent}


@router.post("/allowlist")
async def add_allowlist_token(
    request: AllowlistTokenRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
):
    """Add a custom token (mint address, symbol, decimals) to the caller's wallet allowlist.

    Requires a user JWT and a wallet-enabled deployment (503 otherwise). Returns the stored token."""
    _ensure_wallet_enabled()
    token = await add_custom_allowlist_token(
        user.id,
        mint_address=request.mint_address,
        symbol=request.symbol,
        decimals=request.decimals,
        is_stablecoin=request.is_stablecoin,
    )
    return {"token": token}


@router.post("/webhooks/{provider}")
async def wallet_provider_webhook(
    provider: str,
    request: Request,
    x_wallet_signature: Annotated[str | None, Header(alias="X-Wallet-Signature")] = None,
):
    """Receive a funding settlement callback from a wallet or card provider.

    Not user-authenticated: the body must carry a valid HMAC-SHA256 hex signature in `X-Wallet-Signature`,
    else 401 (also 401 if no webhook secret is configured). Settles the funding intent named in the payload.
    Responds 400 for an unsupported provider, a missing `intent_id` or an unknown intent."""
    _ensure_wallet_enabled()
    body = await request.body()
    if not verify_wallet_webhook_signature(body, x_wallet_signature):
        raise HTTPException(status_code=401, detail="Invalid wallet webhook signature")
    payload = await request.json()
    try:
        result = await settle_funding_intent(provider, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result
