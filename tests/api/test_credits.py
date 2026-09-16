import pytest

from src.api.routes import token as token_routes
from src.core.config import settings
from src.services import credits


def test_calculate_usage_charge_llm_uses_detailed_tokens():
    item = {
        "model_type": "llm",
        "model_id": "openai/gpt-4o-mini",
        "units_used": 1500,
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "cached_input_tokens": 200,
    }

    breakdown = credits.calculate_usage_charge(item)

    expected_provider = (
        ((1000 - 200) / 1_000_000) * 0.15 + (500 / 1_000_000) * 0.60 + (200 / 1_000_000) * 0.075
    )
    expected_billed = expected_provider * settings.billing_markup_multiplier

    assert breakdown.provider_cost_usd == round(expected_provider, 6)
    assert breakdown.billed_cost_usd == round(expected_billed, 6)
    assert breakdown.margin_usd == round(expected_billed - expected_provider, 6)
    assert breakdown.requested_micro_credits > 0
    assert breakdown.pricing_source.startswith("catalog:")


@pytest.mark.anyio
async def test_process_usage_report_marks_insufficient_credits(monkeypatch):
    inserted_logs = []
    settlements = []

    async def fake_log_usage(**kwargs):
        inserted_logs.append(kwargs)
        return f"log-{len(inserted_logs)}"

    async def fake_update_usage_settlement(usage_log_id, *, credits_charged, settlement_status):
        settlements.append(
            {
                "usage_log_id": usage_log_id,
                "credits_charged": credits_charged,
                "settlement_status": settlement_status,
            }
        )

    async def fake_deduct_credits(**kwargs):
        raise ValueError("Insufficient credits")

    async def zero_balance(_user_id):
        return {"balance": 0}

    monkeypatch.setattr(credits, "log_usage", fake_log_usage)
    monkeypatch.setattr(credits, "update_usage_settlement", fake_update_usage_settlement)
    monkeypatch.setattr(credits, "deduct_credits", fake_deduct_credits)
    monkeypatch.setattr(credits, "get_balance", zero_balance)

    result = await credits.process_usage_report(
        user_id="user-1",
        session_id="session-1",
        usage_items=[
            {
                "model_type": "llm",
                "model_id": "openai/gpt-4o-mini",
                "units_used": 1000,
                "prompt_tokens": 600,
                "completion_tokens": 400,
            }
        ],
    )

    assert result["total_credits_requested"] > 0
    assert result["total_credits_charged"] == 0
    assert result["settlement_status"] == "insufficient_credits"
    # The usage still has to be accounted for even when it cannot be charged.
    assert result["unpaid_credits"] == result["total_credits_requested"]
    assert inserted_logs
    assert settlements == [
        {
            "usage_log_id": "log-1",
            "credits_charged": 0,
            "settlement_status": "written_off",
        }
    ]


@pytest.mark.anyio
async def test_token_credit_check_can_fail_closed(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "credits_fail_open_on_check_error", False)

    async def fake_get_balance(_identity):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(token_routes, "get_balance", fake_get_balance)

    response = await auth_client.post(
        "/token",
        json={
            "roomName": "test-room",
            "participantName": "test-user",
        },
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "Credit verification is temporarily unavailable. Please try again shortly."
    )


def test_build_reconciliation_report_summarizes_margin_and_anomalies():
    report = credits.build_reconciliation_report(
        [
            {
                "session_id": "session-1",
                "model_id": "openai/gpt-4o-mini",
                "provider_cost_usd": 0.5,
                "billed_cost_usd": 1.0,
                "margin_usd": 0.5,
                "requested_credits": 1000,
                "credits_charged": 1000,
                "settlement_status": "charged",
                "pricing_source": "catalog:2026-03-20",
            },
            {
                "session_id": "session-1",
                "model_id": "tavily/search",
                "provider_cost_usd": 0.02,
                "billed_cost_usd": 0.04,
                "margin_usd": 0.02,
                "requested_credits": 40,
                "credits_charged": 0,
                "settlement_status": "insufficient_credits",
                "pricing_source": "fallback",
            },
            {
                "session_id": "session-2",
                "model_id": "zep/get_context",
                "provider_cost_usd": 0.0,
                "billed_cost_usd": 0.0,
                "margin_usd": 0.0,
                "requested_credits": 0,
                "credits_charged": 0,
                "settlement_status": "pending",
                "pricing_source": "catalog:2026-03-20",
            },
        ]
    )

    assert report["summary"]["usage_rows"] == 3
    assert report["summary"]["sessions_count"] == 2
    assert report["summary"]["total_provider_cost_usd"] == 0.52
    assert report["summary"]["total_billed_cost_usd"] == 1.04
    assert report["summary"]["total_margin_usd"] == 0.52
    assert report["summary"]["total_requested_credits"] == 1040
    assert report["summary"]["total_charged_credits"] == 1000
    assert report["summary"]["pending_rows"] == 1
    assert report["summary"]["insufficient_rows"] == 1
    assert report["summary"]["fallback_rows"] == 1

    providers = {item["provider"]: item for item in report["provider_breakdown"]}
    assert providers["openai"]["provider_cost_usd"] == 0.5
    assert providers["tavily"]["charged_credits"] == 0
    assert providers["zep"]["usage_rows"] == 1

    sessions = {item["session_id"]: item for item in report["session_breakdown"]}
    assert sessions["session-1"]["settlement_status"] == "insufficient_credits"
    assert sessions["session-2"]["settlement_status"] == "pending"

    anomaly_types = {item["type"]: item["count"] for item in report["anomalies"]}
    assert anomaly_types["pending_settlement"] == 1
    assert anomaly_types["insufficient_credits"] == 1
    assert anomaly_types["fallback_pricing"] == 1
