"""Custody abstraction for kwami Solana wallets."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from src.core.config import settings


@dataclass(slots=True)
class CustodyWalletMaterial:
    public_key: str
    key_ref: str
    provider: str
    metadata: dict


class CustodyError(RuntimeError):
    """Raised when custody operations fail."""


class CustodyService:
    """Managed custody adapter.

    This adapter intentionally never returns private keys to the API layer.
    """

    def __init__(self) -> None:
        self._provider = settings.wallet_custody_provider
        self._secret = settings.wallet_custody_signing_secret or ""

    def create_wallet_material(self, *, user_id: str, kwami_id: str) -> CustodyWalletMaterial:
        if not self._provider:
            raise CustodyError("Wallet custody provider is not configured")
        if self._provider == "mock":
            # Deterministic test-friendly public key stub.
            digest = hashlib.sha256(f"{user_id}:{kwami_id}".encode()).hexdigest()
            return CustodyWalletMaterial(
                public_key=f"mock_{digest[:32]}",
                key_ref=f"mock-key-{digest[:24]}",
                provider="mock",
                metadata={"mode": "mock"},
            )

        if not self._secret:
            raise CustodyError("WALLET_CUSTODY_SIGNING_SECRET is required for custody mode")

        nonce = secrets.token_hex(16)
        raw = f"{self._provider}:{user_id}:{kwami_id}:{nonce}".encode()
        digest = hmac.new(self._secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return CustodyWalletMaterial(
            public_key=f"sol_{digest[:42]}",
            key_ref=f"{self._provider}-key-{digest[:24]}",
            provider=self._provider,
            metadata={"nonce": nonce},
        )


custody_service = CustodyService()
