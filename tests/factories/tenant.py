"""Tenant factory: a user, their kwami, and optional channel / email / wallet rows.

The factory takes a ``FakeDatabase``-compatible object with a ``seed(table, *rows)``
method so the same call sites work against the in-memory fake and against the
real-Postgres harness used by the integration suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from src.core.security import AuthUser

WELCOME_BONUS_MICRO_CREDITS = 500_000


class SupportsSeed(Protocol):
    def seed(self, table: str, *rows: dict[str, Any]) -> list[dict[str, Any]]: ...


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Tenant:
    """One isolated account. Everything a cross-tenant assertion needs."""

    user_id: str
    email: str
    kwami_id: str
    auth_user: AuthUser
    channel: dict[str, Any] | None = None
    email_account: dict[str, Any] | None = None
    wallet: dict[str, Any] | None = None

    @property
    def memory_user_id(self) -> str:
        """The per-kwami memory namespace, as `check_user_access` expects it."""
        return f"kwami_{self.user_id}_{self.kwami_id}"


def make_tenant_factory(db: SupportsSeed) -> Callable[..., Tenant]:
    counter = {"n": 0}

    def factory(
        *,
        email: str | None = None,
        balance: int = WELCOME_BONUS_MICRO_CREDITS,
        kwami_name: str = "Test Kwami",
        with_channel: bool = False,
        with_email_account: bool = False,
        with_wallet: bool = False,
        phone_number: str | None = None,
        role: str = "authenticated",
    ) -> Tenant:
        counter["n"] += 1
        n = counter["n"]
        user_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kwami-test-user-{n}"))
        kwami_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kwami-test-kwami-{n}"))
        address = email or f"tenant{n}@example.com"

        db.seed("auth.users", {"id": user_id, "email": address})
        db.seed(
            "user_credits",
            {
                "user_id": user_id,
                "balance": balance,
                "lifetime_purchased": balance,
                "lifetime_used": 0,
                "updated_at": _now_iso(),
            },
        )
        db.seed(
            "user_kwamis",
            {
                "id": kwami_id,
                "user_id": user_id,
                "name": kwami_name,
                "config": {},
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            },
        )

        channel = None
        if with_channel:
            number = phone_number or f"+1555000{n:04d}"
            channel = db.seed(
                "kwami_channels",
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"kwami-test-channel-{n}")),
                    "user_id": user_id,
                    "kwami_id": kwami_id,
                    "provider": "twilio",
                    "kind": "voice_phone",
                    "phone_number": number,
                    "status": "active",
                    "metadata": {},
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                },
            )[0]

        email_account = None
        if with_email_account:
            username = f"tenant{n}"
            email_account = db.seed(
                "kwami_email_accounts",
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"kwami-test-email-{n}")),
                    "user_id": user_id,
                    "kwami_id": kwami_id,
                    "username": username,
                    "email_address": f"{username}@kwami.io",
                    "status": "active",
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                },
            )[0]

        wallet = None
        if with_wallet:
            wallet = db.seed(
                "kwami_wallets",
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"kwami-test-wallet-{n}")),
                    "user_id": user_id,
                    "kwami_id": kwami_id,
                    "public_key": f"mock_pubkey_{n}",
                    "chain": "solana",
                    "network": "devnet",
                    "status": "active",
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                },
            )[0]

        return Tenant(
            user_id=user_id,
            email=address,
            kwami_id=kwami_id,
            auth_user=AuthUser(
                {"sub": user_id, "email": address, "role": role, "aud": "authenticated"}
            ),
            channel=channel,
            email_account=email_account,
            wallet=wallet,
        )

    return factory
