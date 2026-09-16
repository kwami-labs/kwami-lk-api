"""`POST /credits/usage/report` — auth and replay safety at the HTTP boundary.

This endpoint spends money: it debits whichever account the agent names. It is
guarded by a single shared secret that also authorises every `/internal/*` route,
so the auth assertions matter as much as the arithmetic.
"""

import pytest
from httpx import AsyncClient

from src.core.config import settings

REPORT_PATH = "/credits/usage/report"


def report_body(user_id: str, session_id: str = "session-1") -> dict:
    return {
        "user_id": user_id,
        "session_id": session_id,
        "usage": [
            {
                "model_type": "llm",
                "model_id": "openai/gpt-4o-mini",
                "units_used": 1000,
                "prompt_tokens": 600,
                "completion_tokens": 400,
            }
        ],
    }


@pytest.mark.anyio
async def test_requires_the_agent_key(client: AsyncClient, tenant):
    response = await client.post(REPORT_PATH, json=report_body(tenant.user_id))
    assert response.status_code == 401


@pytest.mark.anyio
async def test_rejects_a_wrong_key(client: AsyncClient, tenant):
    response = await client.post(
        REPORT_PATH,
        json=report_body(tenant.user_id),
        headers={"X-API-Key": "not-the-key"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_rejects_a_key_that_is_a_prefix_of_the_real_one(client: AsyncClient, tenant):
    """Guards the constant-time comparison against a truncated-secret match."""
    response = await client.post(
        REPORT_PATH,
        json=report_body(tenant.user_id),
        headers={"X-API-Key": settings.kwami_api_key[:-1]},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_a_valid_report_is_settled(internal_client: AsyncClient, tenant):
    response = await internal_client.post(REPORT_PATH, json=report_body(tenant.user_id))
    assert response.status_code == 200
    body = response.json()
    assert body["total_credits_charged"] > 0
    assert body["idempotent_replay"] is False


@pytest.mark.anyio
async def test_a_replayed_report_does_not_charge_twice(
    internal_client: AsyncClient, tenant, fake_supabase
):
    body = report_body(tenant.user_id, "session-replay")

    first = await internal_client.post(REPORT_PATH, json=body)
    balance_after_first = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]

    second = await internal_client.post(REPORT_PATH, json=body)
    balance_after_second = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]

    assert first.status_code == second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert balance_after_second == balance_after_first


@pytest.mark.anyio
async def test_an_explicit_idempotency_key_is_honoured(
    internal_client: AsyncClient, tenant, fake_supabase
):
    headers = {"Idempotency-Key": "agent-retry-1"}

    await internal_client.post(REPORT_PATH, json=report_body(tenant.user_id), headers=headers)
    balance = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]

    replay = await internal_client.post(
        REPORT_PATH, json=report_body(tenant.user_id), headers=headers
    )
    after = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
    )["balance"]

    assert replay.json()["idempotent_replay"] is True
    assert after == balance
