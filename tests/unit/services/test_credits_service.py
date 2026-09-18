"""`src.services.credits` — pricing, the ledger, and the reconciliation report.

The settlement path itself (idempotency, partial charge, the under-funded
session) is covered in tests/unit/services/test_usage_settlement.py. This file
covers the rest: the pricing branch table, the paginated reads, the provider
inference, and `build_reconciliation_report`, which is what an operator looks at
when the numbers disagree with a provider invoice.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.services import credits as credits_mod
from src.services.credits import (
    MICRO_CREDITS_PER_CREDIT,
    _apply_billing_policy,
    _calculate_fallback_cost,
    _extract_usage_metadata,
    _find_usage_report,
    _infer_provider,
    _round_usd,
    build_reconciliation_report,
    build_report_key,
    calculate_usage_charge,
    get_balance,
    get_reconciliation_report,
    get_supabase_admin,
    get_transactions,
    get_usage_logs,
    get_usage_logs_for_reconciliation,
    log_usage,
    resolve_ledger_user_id,
    usd_to_micro_credits,
)


class TestUsdToMicroCredits:
    def test_one_dollar_is_a_thousand_credits(self):
        assert usd_to_micro_credits(1.0) == 1000 * MICRO_CREDITS_PER_CREDIT

    def test_any_nonzero_cost_bills_at_least_one_micro_credit(self):
        """Rounding a real cost to zero would make a whole class of usage free."""
        assert usd_to_micro_credits(0.0000000001) == 1

    def test_zero_still_floors_at_one(self):
        assert usd_to_micro_credits(0.0) == 1


def test_round_usd_keeps_six_places():
    assert _round_usd(1.23456789) == 1.234568


class TestExtractUsageMetadata:
    def test_the_accounted_keys_are_dropped(self):
        meta = _extract_usage_metadata(
            {"model_type": "llm", "model_id": "m", "units_used": 5, "session_id": "s"}
        )
        assert meta == {"session_id": "s"}

    def test_none_values_are_dropped(self):
        assert _extract_usage_metadata({"a": None, "b": 1}) == {"b": 1}

    def test_falsey_but_present_values_are_kept(self):
        assert _extract_usage_metadata({"a": 0, "b": False, "c": ""}) == {
            "a": 0,
            "b": False,
            "c": "",
        }


class TestCalculateFallbackCost:
    def test_llm_usage_falls_back_to_a_per_token_rate(self, monkeypatch):
        monkeypatch.setattr(
            credits_mod.settings, "billing_fallback_cost_per_1m_tokens_usd", 2.0, raising=False
        )
        assert _calculate_fallback_cost("llm", 1_000_000) == pytest.approx(2.0)

    @pytest.mark.parametrize("model_type", ["stt", "tts", "realtime", "tool", "memory", "other"])
    def test_everything_else_falls_back_to_free(self, model_type):
        assert _calculate_fallback_cost(model_type, 1_000_000) == 0.0


class TestApplyBillingPolicy:
    def test_the_markup_and_fee_are_applied(self, monkeypatch):
        monkeypatch.setattr(credits_mod, "MARKUP_MULTIPLIER", 2.0)
        monkeypatch.setattr(credits_mod, "FIXED_FEE_USD", 0.5)
        billed, margin, micro = _apply_billing_policy(1.0)
        assert billed == pytest.approx(2.5)
        assert margin == pytest.approx(1.5)
        assert micro == usd_to_micro_credits(2.5)

    @pytest.mark.parametrize("cost", [0.0, -1.0])
    def test_a_non_positive_cost_bills_nothing(self, cost):
        """Zero must stay zero -- the `max(micro, 1)` floor must not apply here."""
        assert _apply_billing_policy(cost) == (0.0, 0.0, 0)


class TestCalculateUsageCharge:
    def test_an_unknown_model_uses_the_fallback_and_says_so(self, caplog):
        with caplog.at_level("WARNING", logger="kwami-api.credits"):
            breakdown = calculate_usage_charge(
                {"model_id": "nope/unknown", "model_type": "llm", "units_used": 1_000_000}
            )
        assert breakdown.pricing_source == "fallback"
        assert breakdown.usage_metadata["fallback_reason"] == "unknown_model"
        assert "No pricing for model" in caplog.text

    def test_token_usage_with_detailed_counts(self):
        model_id = next(m for m, p in credits_mod.ALL_PRICING.items() if p.model_type == "llm")
        breakdown = calculate_usage_charge(
            {
                "model_id": model_id,
                "model_type": "llm",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            }
        )
        assert breakdown.pricing_source.startswith("catalog:")
        assert breakdown.normalized_units_used == 1500, "units default to the token total"
        assert breakdown.provider_cost_usd > 0

    def test_cached_tokens_are_read_from_either_key(self):
        model_id = next(m for m, p in credits_mod.ALL_PRICING.items() if p.model_type == "llm")
        a = calculate_usage_charge(
            {
                "model_id": model_id,
                "model_type": "llm",
                "prompt_tokens": 1000,
                "cached_input_tokens": 500,
            }
        )
        b = calculate_usage_charge(
            {"model_id": model_id, "model_type": "llm", "prompt_tokens": 1000, "cached_tokens": 500}
        )
        assert a.provider_cost_usd == b.provider_cost_usd

    def test_an_explicit_units_used_wins_over_the_token_total(self):
        model_id = next(m for m, p in credits_mod.ALL_PRICING.items() if p.model_type == "llm")
        breakdown = calculate_usage_charge(
            {
                "model_id": model_id,
                "model_type": "llm",
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "units_used": 999,
            }
        )
        assert breakdown.normalized_units_used == 999

    def test_token_usage_with_only_a_total_is_estimated(self):
        """The agent sometimes reports total tokens without the split."""
        model_id = next(m for m, p in credits_mod.ALL_PRICING.items() if p.model_type == "llm")
        breakdown = calculate_usage_charge(
            {"model_id": model_id, "model_type": "llm", "units_used": 1_000_000}
        )
        assert breakdown.pricing_source == "estimated_total_tokens"
        assert breakdown.provider_cost_usd > 0

    def test_audio_usage(self):
        model_id = next(
            m for m, p in credits_mod.ALL_PRICING.items() if p.model_type in ("stt", "tts")
        )
        breakdown = calculate_usage_charge(
            {"model_id": model_id, "model_type": "stt", "units_used": 10}
        )
        assert breakdown.provider_cost_usd > 0
        assert breakdown.pricing_source.startswith("catalog:")

    def test_realtime_usage_with_bare_minutes(self):
        model_id = next(m for m, p in credits_mod.ALL_PRICING.items() if p.model_type == "realtime")
        breakdown = calculate_usage_charge(
            {"model_id": model_id, "model_type": "realtime", "units_used": 10}
        )
        assert breakdown.pricing_source.startswith("catalog:")
        assert breakdown.pricing_source != "catalog:realtime_detailed"

    @pytest.mark.parametrize(
        "field",
        ["audio_input_minutes", "audio_output_minutes", "text_input_tokens", "text_output_tokens"],
    )
    def test_any_detailed_realtime_field_marks_the_source(self, field):
        model_id = next(m for m, p in credits_mod.ALL_PRICING.items() if p.model_type == "realtime")
        breakdown = calculate_usage_charge(
            {"model_id": model_id, "model_type": "realtime", field: 5}
        )
        assert breakdown.pricing_source == "catalog:realtime_detailed"

    def test_external_usage_defaults_to_one_call(self):
        model_id = next(
            m for m, p in credits_mod.ALL_PRICING.items() if p.model_type in ("tool", "memory")
        )
        breakdown = calculate_usage_charge({"model_id": model_id, "model_type": "tool"})
        assert breakdown.normalized_units_used == 1.0

    def test_external_usage_honours_a_request_count(self):
        model_id = next(
            m for m, p in credits_mod.ALL_PRICING.items() if p.model_type in ("tool", "memory")
        )
        breakdown = calculate_usage_charge(
            {"model_id": model_id, "model_type": "tool", "request_count": 7}
        )
        assert breakdown.normalized_units_used == 7.0


class TestGetSupabaseAdmin:
    def test_an_unconfigured_project_is_a_runtime_error(self, monkeypatch):
        monkeypatch.setattr(credits_mod, "_supabase_client", None, raising=False)
        monkeypatch.setattr(credits_mod.settings, "supabase_url", None, raising=False)
        with pytest.raises(RuntimeError, match="SUPABASE_URL and SUPABASE_SECRET_KEY"):
            get_supabase_admin()

    def test_a_missing_key_is_also_a_runtime_error(self, monkeypatch):
        monkeypatch.setattr(credits_mod, "_supabase_client", None, raising=False)
        monkeypatch.setattr(
            credits_mod.settings, "supabase_url", "https://p.supabase.co", raising=False
        )
        monkeypatch.setattr(credits_mod.settings, "supabase_secret_key", None, raising=False)
        with pytest.raises(RuntimeError):
            get_supabase_admin()

    def test_the_client_is_built_once_and_cached(self, monkeypatch):
        built: list[tuple] = []
        monkeypatch.setattr(credits_mod, "_supabase_client", None, raising=False)
        monkeypatch.setattr(
            credits_mod.settings, "supabase_url", "https://p.supabase.co", raising=False
        )
        monkeypatch.setattr(credits_mod.settings, "supabase_secret_key", "sb_secret", raising=False)
        monkeypatch.setattr(
            credits_mod, "create_client", lambda url, key: built.append((url, key)) or "CLIENT"
        )
        assert get_supabase_admin() == "CLIENT"
        assert get_supabase_admin() == "CLIENT"
        assert built == [("https://p.supabase.co", "sb_secret")]


class TestGetBalance:
    @pytest.mark.anyio
    async def test_it_reads_the_existing_row(self, fake_supabase, tenant):
        balance = await get_balance(tenant.user_id)
        assert balance["balance"] == 500_000
        assert balance["lifetime_purchased"] == 500_000
        assert balance["lifetime_used"] == 0

    @pytest.mark.anyio
    async def test_a_user_with_no_row_gets_one_created(self, fake_supabase):
        """The trigger should have made it; a missing row must not 500."""
        balance = await get_balance("11111111-1111-1111-1111-111111111111")
        assert balance == {
            "balance": 0,
            "lifetime_purchased": 0,
            "lifetime_used": 0,
            "updated_at": None,
        }
        assert any(
            str(r["user_id"]) == "11111111-1111-1111-1111-111111111111"
            for r in fake_supabase.db.rows("user_credits")
        )


class TestResolveLedgerUserId:
    def test_a_blank_id_is_rejected(self, fake_supabase):
        with pytest.raises(ValueError, match="user_id is required"):
            resolve_ledger_user_id("   ")

    def test_a_non_uuid_is_returned_unchanged(self, fake_supabase):
        assert resolve_ledger_user_id("agent-alias") == "agent-alias"

    def test_a_kwami_id_maps_to_its_owner(self, fake_supabase, tenant):
        """The agent often sends kwami_id from telephony metadata."""
        assert resolve_ledger_user_id(tenant.kwami_id) == tenant.user_id

    def test_a_user_id_maps_to_itself(self, fake_supabase, tenant):
        assert resolve_ledger_user_id(tenant.user_id) == tenant.user_id

    def test_an_unknown_uuid_is_returned_unchanged(self, fake_supabase):
        unknown = "00000000-0000-0000-0000-000000000000"
        assert resolve_ledger_user_id(unknown) == unknown

    def test_it_strips_whitespace(self, fake_supabase, tenant):
        assert resolve_ledger_user_id(f"  {tenant.kwami_id}  ") == tenant.user_id


class TestLogUsage:
    @pytest.mark.anyio
    async def test_it_inserts_a_pending_row(self, fake_supabase, tenant):
        log_id = await log_usage(
            user_id=tenant.user_id,
            session_id="s1",
            model_type="llm",
            model_id="m",
            units_used=10,
            provider_cost_usd=0.1,
            billed_cost_usd=0.2,
            margin_usd=0.1,
            requested_credits=200,
            pricing_source="catalog",
        )
        (row,) = fake_supabase.db.rows("credit_usage_logs")
        assert str(row["id"]) == str(log_id)
        assert row["settlement_status"] == "pending"
        assert row["credits_charged"] == 0
        assert row["usage_metadata"] == {}

    @pytest.mark.anyio
    async def test_an_insert_that_returns_nothing_is_a_runtime_error(
        self, monkeypatch, fake_supabase, tenant
    ):
        class _T:
            def insert(self, payload):
                return self

            def execute(self):
                return type("R", (), {"data": []})()

        monkeypatch.setattr(
            credits_mod, "get_supabase_admin", lambda: type("C", (), {"table": lambda s, n: _T()})()
        )
        with pytest.raises(RuntimeError, match="Failed to insert credit usage log"):
            await log_usage(
                user_id="u",
                session_id="s",
                model_type="llm",
                model_id="m",
                units_used=1,
                provider_cost_usd=0,
                billed_cost_usd=0,
                margin_usd=0,
                requested_credits=0,
                pricing_source="x",
            )


class TestPaginatedReads:
    def _seed_txns(self, fake_supabase, tenant, n=3):
        for i in range(n):
            fake_supabase.db.seed(
                "credit_transactions",
                {
                    "user_id": tenant.user_id,
                    "amount": 100 + i,
                    "type": "purchase",
                    "idempotency_key": f"k{i}",
                },
            )

    @pytest.mark.anyio
    async def test_transactions_are_returned(self, fake_supabase, tenant):
        self._seed_txns(fake_supabase, tenant)
        assert len(await get_transactions(tenant.user_id)) == 3

    @pytest.mark.anyio
    async def test_transactions_are_scoped_to_the_user(self, fake_supabase, tenant, other_tenant):
        self._seed_txns(fake_supabase, tenant)
        assert await get_transactions(other_tenant.user_id) == []

    @pytest.mark.anyio
    async def test_transactions_paginate(self, fake_supabase, tenant):
        self._seed_txns(fake_supabase, tenant, n=5)
        assert len(await get_transactions(tenant.user_id, limit=2)) == 2
        assert len(await get_transactions(tenant.user_id, limit=2, offset=4)) == 1

    def _seed_logs(self, fake_supabase, tenant, n=3, session_id="s1"):
        for i in range(n):
            fake_supabase.db.seed(
                "credit_usage_logs",
                {
                    "user_id": tenant.user_id,
                    "session_id": session_id,
                    "model_id": "m",
                    "model_type": "llm",
                    "units_used": i,
                    "credits_charged": 0,
                    "created_at": f"2026-03-0{i + 1}T00:00:00+00:00",
                },
            )

    @pytest.mark.anyio
    async def test_usage_logs_are_returned(self, fake_supabase, tenant):
        self._seed_logs(fake_supabase, tenant)
        assert len(await get_usage_logs(tenant.user_id)) == 3

    @pytest.mark.anyio
    async def test_usage_logs_can_be_filtered_by_session(self, fake_supabase, tenant):
        self._seed_logs(fake_supabase, tenant, n=2, session_id="s1")
        self._seed_logs(fake_supabase, tenant, n=1, session_id="s2")
        assert len(await get_usage_logs(tenant.user_id, session_id="s2")) == 1

    @pytest.mark.anyio
    async def test_usage_logs_paginate(self, fake_supabase, tenant):
        self._seed_logs(fake_supabase, tenant, n=5)
        assert len(await get_usage_logs(tenant.user_id, limit=2)) == 2

    @pytest.mark.anyio
    async def test_reconciliation_logs_are_returned(self, fake_supabase, tenant):
        self._seed_logs(fake_supabase, tenant)
        assert len(await get_usage_logs_for_reconciliation(tenant.user_id)) == 3

    @pytest.mark.anyio
    async def test_reconciliation_logs_filter_by_session(self, fake_supabase, tenant):
        self._seed_logs(fake_supabase, tenant, n=2, session_id="s1")
        self._seed_logs(fake_supabase, tenant, n=1, session_id="s2")
        assert len(await get_usage_logs_for_reconciliation(tenant.user_id, session_id="s2")) == 1

    @pytest.mark.anyio
    async def test_reconciliation_logs_filter_by_a_created_window(self, fake_supabase, tenant):
        self._seed_logs(fake_supabase, tenant, n=3)
        rows = await get_usage_logs_for_reconciliation(
            tenant.user_id,
            created_after=datetime(2026, 3, 2, tzinfo=UTC),
            created_before=datetime(2026, 3, 2, 23, tzinfo=UTC),
        )
        assert len(rows) == 1

    @pytest.mark.anyio
    async def test_reconciliation_logs_respect_the_limit(self, fake_supabase, tenant):
        self._seed_logs(fake_supabase, tenant, n=5)
        assert len(await get_usage_logs_for_reconciliation(tenant.user_id, limit=2)) == 2


class TestInferProvider:
    def test_a_priced_model_names_its_provider(self):
        model_id, entry = next(iter(credits_mod.ALL_PRICING.items()))
        assert _infer_provider(model_id) == entry.provider

    def test_an_unpriced_namespaced_id_uses_its_prefix(self):
        assert _infer_provider("someprovider/some-model") == "someprovider"

    def test_a_bare_unknown_id_is_unknown(self):
        assert _infer_provider("mystery") == "unknown"


class TestBuildReportKey:
    def test_the_key_is_stable_across_key_order(self):
        a = build_report_key("u", "s", [{"a": 1, "b": 2}])
        b = build_report_key("u", "s", [{"b": 2, "a": 1}])
        assert a == b

    def test_different_content_gives_a_different_key(self):
        assert build_report_key("u", "s", [{"a": 1}]) != build_report_key("u", "s", [{"a": 2}])

    def test_the_user_and_session_are_part_of_the_key(self):
        assert build_report_key("u1", "s", []) != build_report_key("u2", "s", [])
        assert build_report_key("u", "s1", []) != build_report_key("u", "s2", [])

    def test_unserialisable_values_do_not_crash_it(self):
        assert build_report_key("u", "s", [{"when": datetime(2026, 3, 1, tzinfo=UTC)}])


class TestFindUsageReport:
    def test_it_finds_a_claimed_report(self, fake_supabase):
        fake_supabase.db.seed(
            "usage_reports",
            {
                "report_key": "k1",
                "user_id": "u",
                "session_id": "s",
                "status": "settled",
                "items_count": 1,
                "result": {"ok": True},
            },
        )
        assert _find_usage_report("k1")["status"] == "settled"

    def test_an_unknown_key_is_none(self, fake_supabase):
        assert _find_usage_report("nope") is None


def _anomaly(report: dict, name: str) -> int:
    """`anomalies` is a list of {type, count}, sorted by count, zeroes omitted."""
    for item in report["anomalies"]:
        if item["type"] == name:
            return item["count"]
    return 0


class TestBuildReconciliationReport:
    def test_an_empty_ledger_reports_zeroes(self):
        report = build_reconciliation_report([])
        assert report["summary"]["usage_rows"] == 0
        assert report["summary"]["sessions_count"] == 0
        assert report["provider_breakdown"] == []
        assert report["session_breakdown"] == []
        assert report["anomalies"] == [], "nothing happened, so nothing is anomalous"
        assert report["summary"]["effective_margin_percent"] == 0.0

    def _log(self, **overrides):
        base = {
            "session_id": "s1",
            "model_id": "openai/gpt-4o",
            "provider_cost_usd": 1.0,
            "billed_cost_usd": 2.0,
            "margin_usd": 1.0,
            "requested_credits": 2000,
            "credits_charged": 2000,
            "settlement_status": "charged",
            "pricing_source": "catalog:v1",
        }
        return {**base, **overrides}

    def test_a_charged_row_totals_through(self):
        report = build_reconciliation_report([self._log()])
        summary = report["summary"]
        assert summary["usage_rows"] == 1
        assert summary["sessions_count"] == 1
        assert summary["charged_rows"] == 1
        assert summary["total_provider_cost_usd"] == pytest.approx(1.0)
        assert summary["total_billed_cost_usd"] == pytest.approx(2.0)
        assert summary["total_margin_usd"] == pytest.approx(1.0)

    def test_a_pending_row_is_an_anomaly(self):
        report = build_reconciliation_report([self._log(settlement_status="pending")])
        assert report["summary"]["pending_rows"] == 1
        assert _anomaly(report, "pending_settlement") == 1

    def test_an_insufficient_row_is_an_anomaly(self):
        report = build_reconciliation_report([self._log(settlement_status="insufficient_credits")])
        assert report["summary"]["insufficient_rows"] == 1
        assert _anomaly(report, "insufficient_credits") == 1

    def test_a_fallback_priced_row_is_an_anomaly(self):
        report = build_reconciliation_report([self._log(pricing_source="fallback")])
        assert report["summary"]["fallback_rows"] == 1
        assert _anomaly(report, "fallback_pricing") == 1

    def test_a_zero_cost_row_is_an_anomaly(self):
        report = build_reconciliation_report([self._log(provider_cost_usd=0.0)])
        assert _anomaly(report, "zero_provider_cost") == 1

    def test_charging_without_revenue_is_an_anomaly(self):
        """Credits left the user but nothing was billed -- the worst kind of drift."""
        report = build_reconciliation_report(
            [self._log(provider_cost_usd=0.0, billed_cost_usd=0.0, credits_charged=100)]
        )
        assert _anomaly(report, "charged_without_revenue") == 1

    def test_an_explicit_zero_billed_cost_is_masked_by_the_provider_cost(self):
        """`log.get("billed_cost_usd") or provider_cost` -- 0.0 is falsey.

        A row that genuinely billed nothing while the provider charged something
        is the exact margin leak this report exists to surface, and it reads back
        as `billed == provider`, margin zero, no anomaly. Only a row where BOTH
        are zero trips `charged_without_revenue`. Pinned as it behaves; the fix is
        an explicit `is None` check rather than `or`.
        """
        report = build_reconciliation_report(
            [self._log(provider_cost_usd=1.0, billed_cost_usd=0.0, credits_charged=100)]
        )
        assert report["summary"]["total_billed_cost_usd"] == pytest.approx(1.0)
        assert _anomaly(report, "charged_without_revenue") == 0

    def test_legacy_rows_fall_back_to_cost_usd(self):
        """Older rows only have `cost_usd`."""
        report = build_reconciliation_report(
            [{"session_id": "s", "model_id": "m", "cost_usd": 3.0, "credits_charged": 10}]
        )
        assert report["summary"]["total_provider_cost_usd"] == pytest.approx(3.0)

    def test_a_missing_billed_cost_defaults_to_the_provider_cost(self):
        report = build_reconciliation_report(
            [{"session_id": "s", "model_id": "m", "provider_cost_usd": 3.0}]
        )
        assert report["summary"]["total_billed_cost_usd"] == pytest.approx(3.0)
        assert report["summary"]["total_margin_usd"] == pytest.approx(0.0)

    def test_providers_are_ranked_by_cost(self):
        report = build_reconciliation_report(
            [
                self._log(model_id="openai/gpt-4o", provider_cost_usd=1.0),
                self._log(model_id="anthropic/claude", provider_cost_usd=5.0),
            ]
        )
        costs = [p["provider_cost_usd"] for p in report["provider_breakdown"]]
        assert costs == sorted(costs, reverse=True)

    def test_rows_without_a_session_are_grouped_as_unknown(self):
        report = build_reconciliation_report([self._log(session_id=None)])
        assert report["session_breakdown"][0]["session_id"] == "unknown"
        assert report["summary"]["sessions_count"] == 0, "None is not a session"

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            (["charged"], "charged"),
            (["pending"], "pending"),
            (["charged", "insufficient_credits"], "insufficient_credits"),
            (["charged", "pending"], "pending"),
            (["charged", "partially_charged"], "mixed"),
        ],
    )
    def test_a_sessions_status_is_the_worst_of_its_rows(self, statuses, expected):
        logs = [self._log(settlement_status=s) for s in statuses]
        report = build_reconciliation_report(logs)
        assert report["session_breakdown"][0]["settlement_status"] == expected

    def test_a_session_lists_every_provider_it_touched(self):
        report = build_reconciliation_report(
            [
                self._log(model_id="openai/gpt-4o"),
                self._log(model_id="anthropic/claude"),
            ]
        )
        assert report["session_breakdown"][0]["providers"] == sorted(["openai", "anthropic"])


class TestGetReconciliationReport:
    @pytest.mark.anyio
    async def test_it_reports_how_much_it_scanned(self, fake_supabase, tenant):
        fake_supabase.db.seed(
            "credit_usage_logs",
            {
                "user_id": tenant.user_id,
                "session_id": "s",
                "model_id": "openai/gpt-4o",
                "model_type": "llm",
                "units_used": 1,
                "credits_charged": 1,
            },
        )
        report = await get_reconciliation_report(tenant.user_id)
        assert report["log_rows_scanned"] == 1
        assert report["summary"]["usage_rows"] == 1

    @pytest.mark.anyio
    async def test_an_empty_ledger_reports_nothing_scanned(self, fake_supabase, tenant):
        report = await get_reconciliation_report(tenant.user_id)
        assert report["log_rows_scanned"] == 0


class TestDeductCreditsErrors:
    """The two arms of the `except` around the `deduct_credits` RPC."""

    @pytest.mark.anyio
    async def test_the_sql_insufficient_error_becomes_a_value_error(
        self, monkeypatch, fake_supabase
    ):
        """`deduct_credits` cannot overdraw -- the SQL refuses, Python translates."""

        def rpc(name, params):
            raise RuntimeError("P0001: Insufficient credits for this operation")

        monkeypatch.setattr(
            credits_mod,
            "get_supabase_admin",
            lambda: type("C", (), {"rpc": staticmethod(rpc)})(),
        )
        with pytest.raises(ValueError, match="Insufficient credits"):
            await credits_mod.deduct_credits(user_id="u", amount_micro=100)

    @pytest.mark.anyio
    async def test_any_other_failure_propagates_unchanged(self, monkeypatch, fake_supabase):
        """A connection error must not be reported to the caller as a money problem."""

        def rpc(name, params):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            credits_mod,
            "get_supabase_admin",
            lambda: type("C", (), {"rpc": staticmethod(rpc)})(),
        )
        with pytest.raises(RuntimeError, match="connection refused"):
            await credits_mod.deduct_credits(user_id="u", amount_micro=100)


class TestProcessUsageReportEdges:
    ITEM = {"model_type": "llm", "model_id": "openai/gpt-4o", "units_used": 1000}

    @pytest.mark.anyio
    async def test_a_kwami_id_is_mapped_to_its_owner_and_logged(
        self, fake_supabase, tenant, caplog
    ):
        """The agent reports kwami_id from telephony metadata, not the auth user."""
        with caplog.at_level("INFO", logger="kwami-api.credits"):
            result = await credits_mod.process_usage_report(tenant.kwami_id, "s1", [self.ITEM])
        assert "mapped kwami or alias -> ledger user" in caplog.text
        assert result["settlement_status"] == "charged"
        # The charge landed on the OWNER's ledger, not on the kwami id.
        (log_row,) = fake_supabase.db.rows("credit_usage_logs")
        assert str(log_row["user_id"]) == tenant.user_id

    @pytest.mark.anyio
    async def test_losing_the_claim_race_returns_the_winners_result(
        self, monkeypatch, fake_supabase, tenant
    ):
        """Two concurrent deliveries; UNIQUE(report_key) decides, the loser replays."""
        winner_result = {"total_credits_charged": 42, "new_balance": 1}
        report_key = credits_mod.build_report_key(tenant.user_id, "s1", [self.ITEM])
        fake_supabase.db.seed(
            "usage_reports",
            {
                "report_key": report_key,
                "user_id": tenant.user_id,
                "session_id": "s1",
                "status": "settled",
                "items_count": 1,
                "result": winner_result,
            },
        )

        # Force the insert to fail the way Postgres does, with the row already there.
        real_table = fake_supabase.table

        def table(name):
            handle = real_table(name)
            if name == "usage_reports":
                original_insert = handle.insert

                def insert(payload):
                    raise RuntimeError('duplicate key value violates unique constraint "23505"')

                handle.insert = insert
                assert original_insert is not None
            return handle

        monkeypatch.setattr(
            credits_mod,
            "_find_usage_report",
            _sequence(None, {"result": winner_result}),
        )
        monkeypatch.setattr(
            credits_mod,
            "get_supabase_admin",
            lambda: type("C", (), {"table": staticmethod(table)})(),
        )

        result = await credits_mod.process_usage_report(tenant.user_id, "s1", [self.ITEM])
        assert result["idempotent_replay"] is True
        assert result["total_credits_charged"] == 42

    @pytest.mark.anyio
    async def test_a_claim_failure_that_is_not_a_race_propagates(
        self, monkeypatch, fake_supabase, tenant
    ):
        def table(name):
            class _T:
                """Serves the reads `resolve_ledger_user_id` makes, fails the claim write."""

                def select(self, *a, **k):
                    return self

                def eq(self, *a, **k):
                    return self

                def limit(self, *a, **k):
                    return self

                def execute(self):
                    return type("R", (), {"data": []})()

                def insert(self, payload):
                    raise RuntimeError("connection refused")

            return _T()

        monkeypatch.setattr(credits_mod, "_find_usage_report", lambda key: None)
        monkeypatch.setattr(
            credits_mod,
            "get_supabase_admin",
            lambda: type("C", (), {"table": staticmethod(table)})(),
        )
        with pytest.raises(RuntimeError, match="connection refused"):
            await credits_mod.process_usage_report(tenant.user_id, "s1", [self.ITEM])

    @pytest.mark.anyio
    async def test_a_balance_that_moves_between_read_and_deduct_is_insufficient(
        self, monkeypatch, fake_supabase, tenant
    ):
        """The read said there was money; the SQL disagreed. Do not crash, record it."""

        async def deduct(**kwargs):
            raise ValueError("Insufficient credits")

        monkeypatch.setattr(credits_mod, "deduct_credits", deduct)
        result = await credits_mod.process_usage_report(tenant.user_id, "s1", [self.ITEM])
        assert result["settlement_status"] == "insufficient_credits"
        assert result["total_credits_charged"] == 0

    @pytest.mark.anyio
    async def test_a_report_with_nothing_billable_is_skipped(self, fake_supabase, tenant):
        """`if total_requested_micro_credits > 0` -- a free session charges nothing."""
        free_item = {"model_type": "stt", "model_id": "unpriced/model", "units_used": 0}
        result = await credits_mod.process_usage_report(tenant.user_id, "s-free", [free_item])
        assert result["settlement_status"] == "skipped"
        assert result["total_credits_charged"] == 0
        assert result["new_balance"] == 0

    @pytest.mark.anyio
    async def test_an_empty_item_list_is_skipped(self, fake_supabase, tenant):
        result = await credits_mod.process_usage_report(tenant.user_id, "s-empty", [])
        assert result["settlement_status"] == "skipped"
        assert result["items"] == []


class TestFinalizeUsageReport:
    def test_it_records_the_settlement(self, fake_supabase):
        fake_supabase.db.seed(
            "usage_reports",
            {
                "report_key": "k1",
                "user_id": "u",
                "session_id": "s",
                "status": "pending",
                "items_count": 1,
            },
        )
        credits_mod._finalize_usage_report(
            "k1",
            status="settled",
            requested_micro=10,
            charged_micro=10,
            unpaid_micro=0,
            result={"ok": True},
        )
        (row,) = fake_supabase.db.rows("usage_reports")
        assert row["status"] == "settled"
        assert row["charged_micro"] == 10
        assert row["result"] == {"ok": True}
        assert row["settled_at"] is not None

    def test_a_write_failure_is_swallowed_and_logged(self, monkeypatch, fake_supabase, caplog):
        """Best effort: a completed settlement must not be undone by a bookkeeping error."""

        class _T:
            def update(self, payload):
                return self

            def eq(self, *a):
                return self

            def execute(self):
                raise RuntimeError("update failed")

        monkeypatch.setattr(
            credits_mod,
            "get_supabase_admin",
            lambda: type("C", (), {"table": staticmethod(lambda n: _T())})(),
        )
        with caplog.at_level("ERROR", logger="kwami-api.credits"):
            credits_mod._finalize_usage_report(
                "k1",
                status="settled",
                requested_micro=1,
                charged_micro=1,
                unpaid_micro=0,
                result={},
            )
        assert "Could not record the outcome of usage report" in caplog.text


class TestUnrecognisedPricingShape:
    def test_a_pricing_shape_none_of_the_isinstance_arms_match_bills_zero(self, monkeypatch):
        """The fall-through at the end of the `isinstance` chain.

        Unreachable with the shipped catalog -- every entry's `pricing` is one of
        the four known models, which `test_every_model_type_declares_a_matching_
        pricing_shape` in test_pricing.py asserts. Worth pinning anyway: a fifth
        pricing model added without a branch here bills NOTHING rather than
        failing, which is the quiet kind of revenue loss.
        """
        from pydantic import BaseModel

        from src.services.pricing import ModelPricing

        class FuturePricing(BaseModel):
            per_widget: float = 1.0

        entry = ModelPricing.model_construct(
            model_id="future/model",
            provider="future",
            model_type="llm",
            display_name="Future",
            pricing=FuturePricing(),
        )
        monkeypatch.setitem(credits_mod.ALL_PRICING, "future/model", entry)

        breakdown = calculate_usage_charge(
            {"model_id": "future/model", "model_type": "llm", "units_used": 1000}
        )
        assert breakdown.provider_cost_usd == 0.0
        assert breakdown.requested_micro_credits == 0
        assert breakdown.pricing_source.startswith("catalog:"), "and it does not even flag itself"


class TestExternalPricingUnits:
    def test_a_positive_units_used_is_kept_for_an_external_service(self):
        """`if units_used <= 0` -- the request_count fallback must not override it."""
        model_id = next(
            m for m, p in credits_mod.ALL_PRICING.items() if p.model_type in ("tool", "memory")
        )
        breakdown = calculate_usage_charge(
            {"model_id": model_id, "model_type": "tool", "units_used": 3, "request_count": 99}
        )
        assert breakdown.normalized_units_used == 3.0


def _sequence(*values):
    """A stub that returns each value in turn, repeating the last."""
    calls = {"n": 0}

    def _call(*args, **kwargs):
        index = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return values[index]

    return _call
