"""Usage settlement: charge what the user can pay, and charge it exactly once.

Two money leaks are pinned here:

* Settlement was all-or-nothing. When the session total exceeded the balance the
  user was charged ZERO -- not the balance they had -- so anyone running low got
  an unbounded free session, repeatedly.
* There was no idempotency key of any kind, so a redelivered report from the
  agent debited the user again for the same session.
"""

import pytest

from src.core.config import Settings
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


# --------------------------------------------------------------------------
# what a real session costs
# --------------------------------------------------------------------------


def real_voice_session() -> list[dict]:
    """A 4.4-minute conversation, as it was reported on 2026-11-02."""
    return [
        {
            "model_type": "llm",
            "model_id": "api.anthropic.com/claude-haiku-5-5",
            "units_used": 343984,
            "prompt_tokens": 340782,
            "completion_tokens": 3202,
        },
        {
            "model_type": "stt",
            "model_id": "Deepgram/nova-3",
            "units_used": 4.4175,
            "audio_input_minutes": 4.4175,
        },
        {"model_type": "tts", "model_id": "ElevenLabs/eleven_flash_v2_5", "units_used": 2371},
    ]


def test_a_real_session_is_billed_for_every_part_at_its_real_rate():
    """It was billed $1.38: language at a flat guess, voice at nothing."""
    llm, stt, tts = (credits.calculate_usage_charge(i) for i in real_voice_session())

    assert llm.provider_cost_usd == pytest.approx(0.035679)
    assert stt.provider_cost_usd == pytest.approx(0.034015, abs=1e-6)
    assert tts.provider_cost_usd == pytest.approx(0.09484)
    total = llm.billed_cost_usd + stt.billed_cost_usd + tts.billed_cost_usd
    assert total == pytest.approx(0.32907, abs=1e-4)


def test_every_part_of_a_real_session_is_in_the_catalog():
    for item in real_voice_session():
        charge = credits.calculate_usage_charge(item)
        assert charge.pricing_source != "fallback", item["model_id"]
        assert charge.billed_cost_usd > charge.provider_cost_usd


def test_every_pack_clears_its_costs_at_the_default_markup():
    """Cheaper packs must not be sold at a loss: providers, LiveKit and Stripe all get paid."""
    markup = Settings.model_fields["billing_markup_multiplier"].default
    provider_usd_per_minute = 0.04
    livekit_usd_per_minute = 0.01
    credits_per_minute = provider_usd_per_minute * markup / credits.USD_PER_CREDIT

    for pack in credits.CREDIT_PACKS.values():
        price = pack["price_cents"] / 100
        minutes = pack["credits"] / credits_per_minute
        costs = minutes * (provider_usd_per_minute + livekit_usd_per_minute)
        stripe_fee = price * 0.029 + 0.30
        assert price - costs - stripe_fee > 0, pack["id"]


def test_a_bigger_pack_is_cheaper_per_credit():
    packs = sorted(credits.CREDIT_PACKS.values(), key=lambda p: p["credits"])
    per_credit = [p["price_cents"] / p["credits"] for p in packs]
    assert per_credit == sorted(per_credit, reverse=True)
    assert len(set(per_credit)) == len(per_credit)


def test_a_realtime_model_reported_as_llm_is_read_as_tokens_not_minutes():
    """22873 tokens read as minutes would cost over $1,000 for a short exchange."""
    charge = credits.calculate_usage_charge(
        {
            "model_type": "llm",
            "model_id": "api.openai.com/gpt-realtime",
            "units_used": 22873,
            "prompt_tokens": 22831,
            "completion_tokens": 42,
            "cached_input_tokens": 2816,
        }
    )
    assert charge.provider_cost_usd == pytest.approx(0.081858, abs=1e-6)


@pytest.mark.anyio
async def test_a_browser_session_settles_as_a_tool(fake_supabase, funded_user):
    """The column has no `browser`: the insert raised and the session went unbilled."""
    user_id = funded_user(10_000_000)

    result = await credits.process_usage_report(
        user_id,
        "session-browser",
        [{"model_type": "browser", "model_id": "browserbase/cloud", "units_used": 10.0}],
    )

    assert result["settlement_status"] == "charged"
    assert result["items"][0]["model_type"] == "tool"
    assert result["items"][0]["provider_cost_usd"] == pytest.approx(0.05)


@pytest.mark.anyio
async def test_a_free_item_is_not_recorded_as_forgiven(fake_supabase, funded_user):
    """A memory call that costs nothing used to read `written_off` next to a paid session."""
    user_id = funded_user(10_000_000)
    free = {"model_type": "memory", "model_id": "zep/get_context", "units_used": 1}

    result = await credits.process_usage_report(user_id, "session-free", [llm_item(), free])

    assert [i["settlement_status"] for i in result["items"]] == ["charged", "skipped"]


# --------------------------------------------------------------------------
# someone on their own keys
# --------------------------------------------------------------------------


def own_key_session() -> list[dict]:
    return [{**item, "own_key": True} for item in real_voice_session()]


@pytest.fixture
def connected(fake_supabase, tenant):
    """The providers `real_voice_session` runs on, connected by this user."""
    for provider in ("anthropic", "deepgram", "elevenlabs", "cartesia", "openai"):
        fake_supabase.db.rows("kwami_provider_keys").append(
            {"user_id": tenant.user_id, "provider": provider}
        )


def platform_row(result: dict) -> dict:
    return next(i for i in result["items"] if i["model_id"] == credits.PLATFORM_MODEL_ID)


def test_usage_on_their_own_key_costs_no_energy():
    for item in own_key_session():
        charge = credits.calculate_usage_charge(item)
        assert charge.requested_micro_credits == 0
        assert charge.provider_cost_usd == 0.0
        assert charge.pricing_source == "own_key"
        assert charge.usage_metadata["own_key"] is True


@pytest.mark.anyio
async def test_the_first_session_of_the_day_pays_the_platform(
    fake_supabase, funded_user, connected
):
    user_id = funded_user(10_000_000)

    result = await credits.process_usage_report(
        user_id, "own-1", own_key_session(), session_minutes=10.0
    )

    assert result["total_credits_charged"] == 600_000
    assert platform_row(result)["provider_cost_usd"] == pytest.approx(0.10)


@pytest.mark.anyio
async def test_a_later_session_within_the_included_minutes_pays_nothing(
    fake_supabase, funded_user, connected
):
    user_id = funded_user(10_000_000)
    await credits.process_usage_report(user_id, "own-1", own_key_session(), session_minutes=10.0)

    result = await credits.process_usage_report(
        user_id, "own-2", own_key_session(), session_minutes=10.0
    )

    assert result["total_credits_requested"] == 0


@pytest.mark.anyio
async def test_minutes_past_the_included_ones_are_charged_once(
    fake_supabase, funded_user, connected
):
    user_id = funded_user(10_000_000)
    await credits.process_usage_report(user_id, "own-1", own_key_session(), session_minutes=25.0)

    second = await credits.process_usage_report(
        user_id, "own-2", own_key_session(), session_minutes=10.0
    )
    third = await credits.process_usage_report(
        user_id, "own-3", own_key_session(), session_minutes=10.0
    )

    assert second["total_credits_requested"] == 5 * 20 * 1000
    assert third["total_credits_requested"] == 10 * 20 * 1000


@pytest.mark.anyio
async def test_a_stage_on_our_key_is_still_metered(fake_supabase, funded_user, connected):
    user_id = funded_user(10_000_000)
    llm, stt, tts = real_voice_session()
    mixed = [{**llm, "own_key": True}, {**stt, "own_key": True}, tts]

    result = await credits.process_usage_report(user_id, "mix", mixed, session_minutes=4.4)

    tts_charge = credits.calculate_usage_charge(tts).requested_micro_credits
    assert result["total_credits_requested"] == 600_000 + tts_charge


@pytest.mark.anyio
async def test_a_key_they_never_connected_is_charged_like_ours(fake_supabase, funded_user):
    """The agent says whose key it used, and it may say anything."""
    user_id = funded_user(10_000_000)
    claimed = own_key_session()

    result = await credits.process_usage_report(user_id, "claim", claimed, session_minutes=4.4)

    expected = sum(
        credits.calculate_usage_charge(item).requested_micro_credits
        for item in real_voice_session()
    )
    assert result["total_credits_requested"] == expected
    assert not any(i["model_id"] == credits.PLATFORM_MODEL_ID for i in result["items"])


def test_providers_are_named_as_the_agent_names_them():
    assert credits._provider_of("api.openai.com/tts-1") == "openai"
    assert credits._provider_of("Deepgram/nova-3") == "deepgram"
    assert credits._provider_of("gemini/flash") == "google"
    assert credits._provider_of("no-prefix") == ""


@pytest.mark.anyio
async def test_two_reports_at_once_pay_the_day_once(fake_supabase, funded_user):
    user_id = funded_user(10_000_000)
    today = credits.datetime.now(credits.UTC).date()

    assert await credits._claim_day_fee(user_id, "first", today) is True
    assert await credits._claim_day_fee(user_id, "second", today) is False


@pytest.mark.anyio
async def test_another_failure_claiming_the_day_is_not_swallowed(monkeypatch):
    class Broken:
        def table(self, _name):
            raise RuntimeError("database down")

    monkeypatch.setattr(credits, "get_supabase_admin", lambda: Broken())
    with pytest.raises(RuntimeError, match="database down"):
        await credits._claim_day_fee("u", "s", credits.datetime.now(credits.UTC).date())


@pytest.mark.anyio
async def test_nobody_on_our_keys_pays_the_platform(fake_supabase, funded_user):
    user_id = funded_user(10_000_000)

    result = await credits.process_usage_report(
        user_id, "ours", real_voice_session(), session_minutes=4.4
    )

    assert not any(i["model_id"] == credits.PLATFORM_MODEL_ID for i in result["items"])


def test_a_day_of_included_minutes_clears_its_infrastructure_at_every_pack():
    day_credits = Settings.model_fields["billing_platform_day_credits"].default
    minutes = Settings.model_fields["billing_platform_day_minutes"].default
    infra = Settings.model_fields["billing_infra_per_minute_usd"].default
    extra = Settings.model_fields["billing_platform_extra_minute_credits"].default

    for pack in credits.CREDIT_PACKS.values():
        usd_per_credit = pack["price_cents"] / 100 / pack["credits"]
        fee_share = 0.029 + 0.30 / (pack["price_cents"] / 100)
        net = usd_per_credit * (1 - fee_share)
        assert day_credits * net > minutes * infra, pack["id"]
        assert extra * net > infra, pack["id"]
