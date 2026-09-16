"""Usage settlement: charge what the user can pay, and charge it exactly once.

Two money leaks are pinned here:

* Settlement was all-or-nothing. When the session total exceeded the balance the
  user was charged ZERO -- not the balance they had -- so anyone running low got
  an unbounded free session, repeatedly.
* There was no idempotency key of any kind, so a redelivered report from the
  agent debited the user again for the same session.
"""

import pytest

from src.services import credits


def llm_item(units: int = 1000) -> dict:
    return {
        "model_type": "llm",
        "model_id": "openai/gpt-4o-mini",
        "units_used": units,
        "prompt_tokens": units * 6 // 10,
        "completion_tokens": units * 4 // 10,
    }


@pytest.fixture
def funded_user(fake_supabase, tenant):
    """A tenant whose balance is set precisely, for boundary assertions."""

    def fund(balance_micro: int) -> str:
        row = next(
            r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == tenant.user_id
        )
        row["balance"] = balance_micro
        return tenant.user_id

    return fund


# --------------------------------------------------------------------------
# the clamp
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_fully_funded_session_is_charged_in_full(fake_supabase, funded_user):
    user_id = funded_user(10_000_000)

    result = await credits.process_usage_report(user_id, "session-full", [llm_item()])

    assert result["settlement_status"] == "charged"
    assert result["total_credits_charged"] == result["total_credits_requested"]
    assert result["unpaid_credits"] == 0


@pytest.mark.anyio
async def test_an_underfunded_session_charges_the_remaining_balance(fake_supabase, funded_user):
    """The leak: this used to charge 0 and hand over the session for free."""
    result_probe = credits.calculate_usage_charge(llm_item())
    requested = result_probe.requested_micro_credits
    assert requested > 2, "need a total large enough to sit above the balance"

    user_id = funded_user(requested - 1)
    result = await credits.process_usage_report(user_id, "session-short", [llm_item()])

    assert result["total_credits_requested"] == requested
    assert result["total_credits_charged"] == requested - 1, "charge what they had"
    assert result["total_credits_charged"] > 0, "never give the session away"
    assert result["unpaid_credits"] == 1
    assert result["settlement_status"] == "partially_charged"

    balance = next(r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == user_id)[
        "balance"
    ]
    assert balance == 0, "spend down to zero, never below"


@pytest.mark.anyio
async def test_a_zero_balance_charges_nothing_but_records_the_debt(fake_supabase, funded_user):
    user_id = funded_user(0)

    result = await credits.process_usage_report(user_id, "session-broke", [llm_item()])

    assert result["total_credits_charged"] == 0
    assert result["settlement_status"] == "insufficient_credits"
    assert result["unpaid_credits"] == result["total_credits_requested"]
    assert all(item["settlement_status"] == "written_off" for item in result["items"])


@pytest.mark.anyio
async def test_per_item_charges_sum_to_the_ledger_entry(fake_supabase, funded_user):
    """Per-row settlement must reconcile against the single deduction."""
    user_id = funded_user(10_000_000)

    result = await credits.process_usage_report(
        user_id, "session-many", [llm_item(500), llm_item(700), llm_item(900)]
    )

    assert sum(i["credits_charged"] for i in result["items"]) == result["total_credits_charged"]


@pytest.mark.anyio
async def test_the_balance_never_goes_negative(fake_supabase, funded_user):
    user_id = funded_user(5)
    await credits.process_usage_report(user_id, "session-neg", [llm_item(50_000)])

    balance = next(r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == user_id)[
        "balance"
    ]
    assert balance >= 0


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_replaying_a_report_does_not_charge_twice(fake_supabase, funded_user):
    user_id = funded_user(10_000_000)
    items = [llm_item()]

    first = await credits.process_usage_report(user_id, "session-replay", items)
    balance_after_first = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == user_id
    )["balance"]

    second = await credits.process_usage_report(user_id, "session-replay", items)
    balance_after_second = next(
        r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == user_id
    )["balance"]

    assert second.get("idempotent_replay") is True
    assert balance_after_second == balance_after_first, "one session, one charge"
    assert second["total_credits_charged"] == first["total_credits_charged"]


@pytest.mark.anyio
async def test_an_explicit_idempotency_key_is_honoured(fake_supabase, funded_user):
    user_id = funded_user(10_000_000)

    await credits.process_usage_report(
        user_id, "session-key", [llm_item()], idempotency_key="agent-supplied-key"
    )
    before = next(r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == user_id)[
        "balance"
    ]

    # Different content, same key -> still a replay.
    replay = await credits.process_usage_report(
        user_id, "session-key", [llm_item(9999)], idempotency_key="agent-supplied-key"
    )
    after = next(r for r in fake_supabase.db.rows("user_credits") if r["user_id"] == user_id)[
        "balance"
    ]

    assert replay.get("idempotent_replay") is True
    assert after == before


@pytest.mark.anyio
async def test_different_sessions_are_charged_separately(fake_supabase, funded_user):
    """Idempotency must not swallow genuinely distinct reports."""
    user_id = funded_user(10_000_000)

    first = await credits.process_usage_report(user_id, "session-a", [llm_item()])
    second = await credits.process_usage_report(user_id, "session-b", [llm_item()])

    assert not second.get("idempotent_replay")
    assert second["total_credits_charged"] == first["total_credits_charged"] > 0
