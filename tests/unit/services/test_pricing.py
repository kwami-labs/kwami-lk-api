"""`src.services.pricing` — measured provider usage to raw provider cost.

This is the bottom of the money stack: the ledger multiplies these numbers by
the markup, so an error here is an error in every invoice. The arms that matter
are the fallbacks — a cached-token price that is absent, an audio model priced
per-character rather than per-minute, and a realtime session reported as bare
minutes because nothing finer was measured.
"""

from __future__ import annotations

import pytest

from src.services.pricing import (
    ALL_PRICING,
    EXTERNAL_PRICING,
    LLM_PRICING,
    REALTIME_PRICING,
    STT_PRICING,
    TTS_PRICING,
    AudioPricing,
    ExternalPricing,
    RealtimePricing,
    TokenPricing,
    calculate_audio_cost,
    calculate_external_cost,
    calculate_realtime_cost,
    calculate_token_cost,
    get_model_pricing,
    get_pricing_by_type,
)


class TestCalculateTokenCost:
    def test_the_plain_case_is_input_plus_output(self):
        p = TokenPricing(input_per_1m=2.0, output_per_1m=10.0)
        assert calculate_token_cost(p, 1_000_000, 1_000_000) == pytest.approx(12.0)

    def test_zero_usage_is_free(self):
        assert calculate_token_cost(TokenPricing(input_per_1m=2.0, output_per_1m=10.0), 0, 0) == 0.0

    def test_cached_tokens_are_billed_at_the_cached_rate(self):
        p = TokenPricing(input_per_1m=2.0, output_per_1m=10.0, cached_input_per_1m=0.5)
        # 1M prompt of which 400k cached: 600k @ 2.0 + 400k @ 0.5
        assert calculate_token_cost(p, 1_000_000, 0, 400_000) == pytest.approx(1.2 + 0.2)

    def test_cached_tokens_fall_back_to_the_input_rate_when_unpriced(self):
        """No cached price means the provider does not discount them -- do not zero them."""
        p = TokenPricing(input_per_1m=2.0, output_per_1m=10.0)
        assert calculate_token_cost(p, 1_000_000, 0, 400_000) == pytest.approx(2.0)

    def test_cached_tokens_are_never_double_counted(self):
        p = TokenPricing(input_per_1m=2.0, output_per_1m=10.0, cached_input_per_1m=0.0)
        assert calculate_token_cost(p, 1_000_000, 0, 1_000_000) == pytest.approx(0.0)

    def test_more_cached_than_prompt_tokens_does_not_go_negative(self):
        """A provider over-reporting cached tokens must not produce a credit."""
        p = TokenPricing(input_per_1m=2.0, output_per_1m=10.0, cached_input_per_1m=0.5)
        cost = calculate_token_cost(p, 100, 0, 1_000_000)
        assert cost >= 0

    def test_a_negative_cached_count_is_clamped_to_zero(self):
        p = TokenPricing(input_per_1m=2.0, output_per_1m=10.0, cached_input_per_1m=0.5)
        assert calculate_token_cost(p, 1_000_000, 0, -5) == pytest.approx(2.0)


class TestCalculateAudioCost:
    def test_per_minute_pricing(self):
        assert calculate_audio_cost(AudioPricing(per_minute=0.02), 30) == pytest.approx(0.6)

    def test_per_character_pricing(self):
        assert calculate_audio_cost(AudioPricing(per_1m_characters=15.0), 500_000) == pytest.approx(
            7.5
        )

    def test_per_minute_wins_when_both_are_set(self):
        p = AudioPricing(per_minute=0.02, per_1m_characters=15.0)
        assert calculate_audio_cost(p, 30) == pytest.approx(0.6)

    def test_an_unpriced_model_is_free_rather_than_an_error(self):
        """Billing zero is recoverable; a 500 in the settlement path is not."""
        assert calculate_audio_cost(AudioPricing(), 1000) == 0.0

    def test_zero_usage_is_free(self):
        assert calculate_audio_cost(AudioPricing(per_minute=0.02), 0) == 0.0


class TestCalculateRealtimeCost:
    def test_detailed_audio_is_billed_per_direction(self):
        p = RealtimePricing(audio_input_per_minute=0.1, audio_output_per_minute=0.2)
        cost = calculate_realtime_cost(p, audio_input_minutes=2, audio_output_minutes=3)
        assert cost == pytest.approx(0.2 + 0.6)

    def test_text_units_are_added_when_priced(self):
        p = RealtimePricing(
            audio_input_per_minute=0.1,
            audio_output_per_minute=0.2,
            text_input_per_1m=5.0,
            text_output_per_1m=20.0,
        )
        cost = calculate_realtime_cost(
            p, audio_input_minutes=1, text_input_tokens=1_000_000, text_output_tokens=1_000_000
        )
        assert cost == pytest.approx(0.1 + 5.0 + 20.0)

    def test_text_units_are_ignored_when_unpriced(self):
        p = RealtimePricing(audio_input_per_minute=0.1, audio_output_per_minute=0.2)
        cost = calculate_realtime_cost(
            p, audio_input_minutes=1, text_input_tokens=1_000_000, text_output_tokens=1_000_000
        )
        assert cost == pytest.approx(0.1)

    def test_text_only_usage_still_counts_as_detailed(self):
        p = RealtimePricing(
            audio_input_per_minute=0.1, audio_output_per_minute=0.2, text_input_per_1m=5.0
        )
        cost = calculate_realtime_cost(p, text_input_tokens=1_000_000, fallback_minutes=99)
        assert cost == pytest.approx(5.0), "fallback must not apply once anything is measured"

    def test_bare_minutes_fall_back_to_the_average_of_both_directions(self):
        p = RealtimePricing(audio_input_per_minute=0.1, audio_output_per_minute=0.3)
        assert calculate_realtime_cost(p, fallback_minutes=10) == pytest.approx(10 * 0.2)

    def test_no_units_at_all_is_free(self):
        p = RealtimePricing(audio_input_per_minute=0.1, audio_output_per_minute=0.3)
        assert calculate_realtime_cost(p) == 0.0

    def test_only_the_output_direction_measured(self):
        p = RealtimePricing(audio_input_per_minute=0.1, audio_output_per_minute=0.3)
        assert calculate_realtime_cost(p, audio_output_minutes=2) == pytest.approx(0.6)

    def test_only_the_output_text_direction_priced(self):
        p = RealtimePricing(
            audio_input_per_minute=0.1, audio_output_per_minute=0.3, text_output_per_1m=20.0
        )
        assert calculate_realtime_cost(p, text_output_tokens=1_000_000) == pytest.approx(20.0)


class TestCalculateExternalCost:
    def test_it_is_a_per_call_multiple(self):
        assert calculate_external_cost(ExternalPricing(per_call=0.008), 25) == pytest.approx(0.2)

    def test_zero_calls_are_free(self):
        assert calculate_external_cost(ExternalPricing(per_call=0.008), 0) == 0.0


class TestGetPricingByType:
    @pytest.mark.parametrize(
        ("model_type", "table"),
        [
            ("llm", LLM_PRICING),
            ("stt", STT_PRICING),
            ("tts", TTS_PRICING),
            ("realtime", REALTIME_PRICING),
        ],
    )
    def test_each_simple_type_returns_its_whole_table(self, model_type, table):
        assert get_pricing_by_type(model_type) is table

    @pytest.mark.parametrize("model_type", ["tool", "memory"])
    def test_the_external_types_are_filtered_out_of_one_table(self, model_type):
        result = get_pricing_by_type(model_type)
        assert result, f"no {model_type} entries are priced"
        assert all(p.model_type == model_type for p in result.values())
        assert set(result) <= set(EXTERNAL_PRICING)

    def test_tool_and_memory_do_not_overlap(self):
        assert not set(get_pricing_by_type("tool")) & set(get_pricing_by_type("memory"))

    def test_an_unmatched_type_returns_none_despite_the_annotation(self):
        """The `match` has no `case _`, so it falls off the end.

        The signature promises `dict[str, ModelPricing]` and the `Literal` keeps
        static callers honest, but nothing enforces it at runtime: a value that
        reaches here from parsed JSON or a database column gets `None`, and the
        `for ... in None` at the call site is where it actually fails. Pinned as
        it behaves; a `case _: raise ValueError` would be the fix.
        """
        assert get_pricing_by_type("not-a-model-type") is None


class TestGetModelPricing:
    def test_a_known_model_is_found(self):
        known = next(iter(LLM_PRICING))
        assert get_model_pricing(known) is ALL_PRICING[known]

    def test_an_unknown_model_is_none(self):
        assert get_model_pricing("nope/not-a-model") is None

    def test_every_table_is_reachable_through_the_combined_index(self):
        for table in (LLM_PRICING, STT_PRICING, TTS_PRICING, REALTIME_PRICING, EXTERNAL_PRICING):
            for model_id in table:
                assert get_model_pricing(model_id) is not None


class TestTableIntegrity:
    def test_every_entry_is_keyed_by_its_own_model_id(self):
        for model_id, pricing in ALL_PRICING.items():
            assert pricing.model_id == model_id

    def test_no_price_is_negative(self):
        for pricing in ALL_PRICING.values():
            for value in pricing.pricing.model_dump().values():
                if isinstance(value, int | float):
                    assert value >= 0, f"{pricing.model_id} has a negative price"

    def test_every_model_type_declares_a_matching_pricing_shape(self):
        expected = {
            "llm": TokenPricing,
            "stt": AudioPricing,
            "tts": AudioPricing,
            "realtime": RealtimePricing,
            "tool": ExternalPricing,
            "memory": ExternalPricing,
        }
        for pricing in ALL_PRICING.values():
            assert isinstance(pricing.pricing, expected[pricing.model_type]), pricing.model_id

    def test_every_audio_model_is_actually_priced(self):
        """An AudioPricing with neither field set silently bills zero forever."""
        for pricing in list(STT_PRICING.values()) + list(TTS_PRICING.values()):
            audio = pricing.pricing
            assert audio.per_minute is not None or audio.per_1m_characters is not None, (
                f"{pricing.model_id} has no price at all"
            )
