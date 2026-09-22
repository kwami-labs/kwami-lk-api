"""Ownership resolution, webhook idempotency, and model-metadata inference.

`resolve_owned_kwami` is the single seam four duplicate implementations were
collapsed into; `claim_event` / `already_in_ledger` are the two independent
guards that stop a redelivered webhook crediting twice.
"""

from __future__ import annotations

import pytest
from postgrest.exceptions import APIError

from src.core.errors import KwamiNotFoundError
from src.services import idempotency, kwamis
from src.services.idempotency import (
    already_in_ledger,
    claim_event,
    complete_event,
    insert_or_existing,
    ledger_key,
)
from src.services.kwamis import KWAMI_COLUMNS, resolve_kwami, resolve_owned_kwami
from src.services.models import get_default_metadata, get_model_metadata


class TestResolveOwnedKwami:
    @pytest.mark.anyio
    async def test_it_returns_the_owned_row(self, fake_supabase, tenant):
        row = await resolve_owned_kwami(tenant.user_id, tenant.kwami_id)
        assert row["id"] == tenant.kwami_id
        assert row["user_id"] == tenant.user_id

    @pytest.mark.anyio
    async def test_another_users_kwami_is_indistinguishable_from_a_missing_one(
        self, fake_supabase, tenant, other_tenant
    ):
        """No existence oracle: both arms raise the same error."""
        with pytest.raises(KwamiNotFoundError):
            await resolve_owned_kwami(other_tenant.user_id, tenant.kwami_id)
        with pytest.raises(KwamiNotFoundError):
            await resolve_owned_kwami(other_tenant.user_id, "00000000-0000-0000-0000-000000000000")

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("user_id", "kwami_id"),
        [("", "k1"), ("u1", ""), ("", ""), (None, "k1"), ("u1", None)],
    )
    async def test_a_blank_id_short_circuits_without_a_query(
        self, monkeypatch, fake_supabase, user_id, kwami_id
    ):
        def explode():
            raise AssertionError("must not reach the database")

        monkeypatch.setattr(kwamis, "get_supabase_admin", explode)
        with pytest.raises(KwamiNotFoundError):
            await resolve_owned_kwami(user_id, kwami_id)

    @pytest.mark.anyio
    async def test_the_default_column_set_carries_the_config_blob(self, fake_supabase, tenant):
        assert "config" in KWAMI_COLUMNS
        assert "config" in await resolve_owned_kwami(tenant.user_id, tenant.kwami_id)

    @pytest.mark.anyio
    async def test_the_column_set_is_passed_through_to_select(
        self, monkeypatch, fake_supabase, tenant
    ):
        """Asserted at the seam: FakeSupabase does not project, PostgREST does."""
        seen: list[str] = []
        monkeypatch.setattr(
            kwamis, "get_supabase_admin", _select_spy(seen, [{"id": tenant.kwami_id}])
        )
        await resolve_owned_kwami(tenant.user_id, tenant.kwami_id, columns="id")
        assert seen == ["id"]


class TestResolveKwami:
    @pytest.mark.anyio
    async def test_it_finds_a_kwami_without_a_user_filter(self, fake_supabase, tenant):
        """For /internal/* routes, where there is no end user to scope by."""
        assert (await resolve_kwami(tenant.kwami_id))["id"] == tenant.kwami_id

    @pytest.mark.anyio
    async def test_a_missing_kwami_raises(self, fake_supabase):
        with pytest.raises(KwamiNotFoundError):
            await resolve_kwami("00000000-0000-0000-0000-000000000000")

    @pytest.mark.anyio
    async def test_a_blank_id_short_circuits(self, monkeypatch, fake_supabase):
        def explode():
            raise AssertionError("must not reach the database")

        monkeypatch.setattr(kwamis, "get_supabase_admin", explode)
        with pytest.raises(KwamiNotFoundError):
            await resolve_kwami("")

    @pytest.mark.anyio
    async def test_the_column_set_is_passed_through_to_select(
        self, monkeypatch, fake_supabase, tenant
    ):
        seen: list[str] = []
        monkeypatch.setattr(
            kwamis, "get_supabase_admin", _select_spy(seen, [{"id": tenant.kwami_id}])
        )
        await resolve_kwami(tenant.kwami_id, columns="id, name")
        assert seen == ["id, name"]


def _select_spy(seen: list[str], data: list[dict]):
    class _T:
        def select(self, columns):
            seen.append(columns)
            return self

        def eq(self, *a):
            return self

        def limit(self, n):
            return self

        async def execute(self):
            return type("R", (), {"data": data})()

    class _C:
        def table(self, name):
            return _T()

    return lambda: _C()


class TestClaimEvent:
    @pytest.mark.anyio
    async def test_a_first_delivery_is_claimed_and_recorded(self, fake_supabase):
        assert await claim_event("stripe", "evt_1", "checkout.session.completed", {"a": 1}) is True
        rows = fake_supabase.db.rows("payment_events")
        assert len(rows) == 1
        assert rows[0]["provider"] == "stripe"
        assert rows[0]["event_id"] == "evt_1"
        assert rows[0]["status"] == "received"
        assert rows[0]["payload"] == {"a": 1}

    @pytest.mark.anyio
    async def test_a_redelivery_is_refused(self, fake_supabase):
        assert await claim_event("stripe", "evt_1", "t") is True
        assert await claim_event("stripe", "evt_1", "t") is False
        assert len(fake_supabase.db.rows("payment_events")) == 1

    @pytest.mark.anyio
    async def test_the_same_id_from_a_different_provider_is_a_different_event(self, fake_supabase):
        assert await claim_event("stripe", "evt_1", "t") is True
        assert await claim_event("twilio", "evt_1", "t") is True

    @pytest.mark.anyio
    async def test_a_missing_payload_is_stored_as_an_empty_object(self, fake_supabase):
        await claim_event("stripe", "evt_1", "t")
        assert fake_supabase.db.rows("payment_events")[0]["payload"] == {}

    @pytest.mark.anyio
    async def test_an_event_with_no_id_is_processed_but_warned_about(self, fake_supabase, caplog):
        """Dropping a real payment is worse than a possible double -- but be loud."""
        with caplog.at_level("WARNING", logger="kwami-api.idempotency"):
            assert await claim_event("stripe", "", "t") is True
        assert "carried no event id" in caplog.text
        assert fake_supabase.db.rows("payment_events") == []

    @pytest.mark.anyio
    async def test_a_non_unique_failure_propagates(self, monkeypatch, fake_supabase):
        """A connection error must not be read as "already delivered"."""
        monkeypatch.setattr(
            idempotency,
            "get_supabase_admin",
            lambda: _insert_raising(RuntimeError("connection refused")),
        )
        with pytest.raises(RuntimeError, match="connection refused"):
            await claim_event("stripe", "evt_1", "t")


class TestInsertOrExisting:
    """A provider redelivery has to come back as the stored row, not a 23505.

    The event tables are uniquely indexed on the provider's own id, so a duplicate
    row was never possible -- but the violation escaped as a 500, and both Twilio
    and SendGrid retry a 5xx, so the retry could never succeed.
    """

    PAYLOAD = {"channel_id": "c", "direction": "inbound", "provider_call_sid": "CA1"}

    def _payload(self, tenant, **overrides):
        return {
            **self.PAYLOAD,
            "user_id": tenant.user_id,
            "kwami_id": tenant.kwami_id,
            **overrides,
        }

    @pytest.mark.anyio
    async def test_a_first_insert_returns_the_new_row(self, fake_supabase, tenant):
        row = await insert_or_existing(
            "kwami_call_events", self._payload(tenant), conflict_column="provider_call_sid"
        )
        assert row["provider_call_sid"] == "CA1"
        assert len(fake_supabase.db.rows("kwami_call_events")) == 1

    @pytest.mark.anyio
    async def test_a_redelivery_returns_the_stored_row(self, fake_supabase, tenant, caplog):
        first = await insert_or_existing(
            "kwami_call_events", self._payload(tenant), conflict_column="provider_call_sid"
        )
        with caplog.at_level("INFO", logger="kwami-api.idempotency"):
            second = await insert_or_existing(
                "kwami_call_events", self._payload(tenant), conflict_column="provider_call_sid"
            )
        assert second["id"] == first["id"]
        assert len(fake_supabase.db.rows("kwami_call_events")) == 1
        assert "Redelivery" in caplog.text

    @pytest.mark.anyio
    async def test_a_null_conflict_value_is_not_deduplicated(self, fake_supabase, tenant):
        """The unique indexes are partial (`WHERE ... IS NOT NULL`)."""
        for _ in range(2):
            await insert_or_existing(
                "kwami_call_events",
                self._payload(tenant, provider_call_sid=None),
                conflict_column="provider_call_sid",
            )
        assert len(fake_supabase.db.rows("kwami_call_events")) == 2

    @pytest.mark.anyio
    async def test_a_non_unique_failure_propagates(self, monkeypatch, fake_supabase):
        """A connection error must not be mistaken for a redelivery."""
        monkeypatch.setattr(
            idempotency,
            "get_supabase_admin",
            lambda: _insert_raising(RuntimeError("connection refused")),
        )
        with pytest.raises(RuntimeError, match="connection refused"):
            await insert_or_existing(
                "kwami_call_events",
                {"provider_call_sid": "CA1"},
                conflict_column="provider_call_sid",
            )

    @pytest.mark.anyio
    async def test_a_unique_violation_with_no_conflict_value_still_propagates(
        self, monkeypatch, fake_supabase
    ):
        """Some *other* unique index was violated; there is nothing to look up."""
        monkeypatch.setattr(
            idempotency,
            "get_supabase_admin",
            lambda: _insert_raising(APIError({"message": "duplicate key", "code": "23505"})),
        )
        with pytest.raises(APIError):
            await insert_or_existing(
                "kwami_call_events",
                {"provider_call_sid": None},
                conflict_column="provider_call_sid",
            )

    @pytest.mark.anyio
    async def test_a_vanished_conflicting_row_is_none(self, monkeypatch, fake_supabase):
        """The winner was deleted between the failed insert and the re-read."""

        class _Client:
            def table(self, name):
                return self

            def insert(self, payload):
                self._mode = "insert"
                return self

            def select(self, *a, **k):
                self._mode = "select"
                return self

            def eq(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            async def execute(self):
                if self._mode == "insert":
                    raise APIError({"message": "duplicate key", "code": "23505"})
                return type("R", (), {"data": []})()

        monkeypatch.setattr(idempotency, "get_supabase_admin", _Client)
        assert (
            await insert_or_existing(
                "kwami_call_events",
                {"provider_call_sid": "CA1"},
                conflict_column="provider_call_sid",
            )
            is None
        )

    @pytest.mark.anyio
    async def test_a_bare_dict_response_is_returned_as_the_row(self, monkeypatch, fake_supabase):
        """PostgREST answers with an object rather than a list for some shapes."""

        class _Client:
            def table(self, name):
                return self

            def insert(self, payload):
                return self

            async def execute(self):
                return type("R", (), {"data": {"id": "x"}})()

        monkeypatch.setattr(idempotency, "get_supabase_admin", _Client)
        assert await insert_or_existing("t", {"c": 1}, conflict_column="c") == {"id": "x"}


class TestCompleteEvent:
    @pytest.mark.anyio
    async def test_it_records_the_outcome(self, fake_supabase):
        await claim_event("stripe", "evt_1", "t")
        await complete_event("stripe", "evt_1", status="processed", result={"credited": 100})
        row = fake_supabase.db.rows("payment_events")[0]
        assert row["status"] == "processed"
        assert row["result"] == {"credited": 100}
        assert row["error"] is None
        assert row["processed_at"] is not None

    @pytest.mark.anyio
    async def test_it_records_a_failure(self, fake_supabase):
        await claim_event("stripe", "evt_1", "t")
        await complete_event("stripe", "evt_1", status="failed", error="boom")
        row = fake_supabase.db.rows("payment_events")[0]
        assert row["status"] == "failed"
        assert row["error"] == "boom"
        assert row["result"] == {}

    @pytest.mark.anyio
    async def test_it_is_a_no_op_without_an_event_id(self, monkeypatch, fake_supabase):
        def explode():
            raise AssertionError("must not reach the database")

        monkeypatch.setattr(idempotency, "get_supabase_admin", explode)
        await complete_event("stripe", "")

    @pytest.mark.anyio
    async def test_a_write_failure_is_swallowed_and_logged(
        self, monkeypatch, fake_supabase, caplog
    ):
        """Best effort: recording the outcome must never mask the outcome itself."""
        monkeypatch.setattr(
            idempotency, "get_supabase_admin", lambda: _update_raising(RuntimeError("down"))
        )
        with caplog.at_level("ERROR", logger="kwami-api.idempotency"):
            await complete_event("stripe", "evt_1")
        assert "Could not record outcome" in caplog.text


class TestLedgerKey:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (("stripe:session", "cs_1"), "stripe:session:cs_1"),
            (("stripe:refund", "ch_1", "re_1"), "stripe:refund:ch_1:re_1"),
            (("wallet:intent", "uuid-1"), "wallet:intent:uuid-1"),
            (("usage:report", "k"), "usage:report:k"),
            (("bonus:welcome", "u1"), "bonus:welcome:u1"),
            (("ns",), "ns"),
        ],
    )
    def test_it_joins_the_namespace_and_parts(self, args, expected):
        assert ledger_key(*args) == expected

    def test_non_string_parts_are_coerced(self):
        assert ledger_key("ns", 1, None) == "ns:1:None"


class TestAlreadyInLedger:
    @pytest.mark.anyio
    async def test_it_finds_an_existing_row(self, fake_supabase, tenant):
        fake_supabase.db.seed(
            "credit_transactions",
            {
                "user_id": tenant.user_id,
                "amount": 100,
                "type": "purchase",
                "idempotency_key": "stripe:session:cs_1",
            },
        )
        assert await already_in_ledger("stripe:session:cs_1") is True

    @pytest.mark.anyio
    async def test_it_is_false_for_an_unused_key(self, fake_supabase):
        assert await already_in_ledger("stripe:session:nope") is False

    @pytest.mark.anyio
    async def test_a_blank_key_short_circuits(self, monkeypatch, fake_supabase):
        def explode():
            raise AssertionError("must not reach the database")

        monkeypatch.setattr(idempotency, "get_supabase_admin", explode)
        assert await already_in_ledger("") is False


def _insert_raising(exc: Exception):
    class _T:
        def insert(self, payload):
            return self

        async def execute(self):
            raise exc

    class _C:
        def table(self, name):
            return _T()

    return _C()


def _update_raising(exc: Exception):
    class _T:
        def update(self, payload):
            return self

        def eq(self, *a):
            return self

        async def execute(self):
            raise exc

    class _C:
        def table(self, name):
            return _T()

    return _C()


@pytest.mark.anyio
async def test_a_unique_violation_is_recognised_as_a_duplicate(monkeypatch, fake_supabase):
    err = APIError(
        {"message": "duplicate key value", "code": "23505", "hint": None, "details": None}
    )
    monkeypatch.setattr(idempotency, "get_supabase_admin", lambda: _insert_raising(err))
    assert await claim_event("stripe", "evt_1", "t") is False


class TestModelMetadata:
    def test_a_known_model_returns_its_curated_entry(self):
        from src.services.models import LLM_METADATA

        known = next(iter(LLM_METADATA))
        assert get_model_metadata(known) is LLM_METADATA[known]

    def test_an_unknown_model_returns_none(self):
        assert get_model_metadata("nope/not-a-model") is None


class TestDefaultMetadata:
    def test_a_namespaced_id_is_titled_from_its_last_segment(self):
        m = get_default_metadata("openai/gpt-4o-mini", "openai")
        assert m.display_name == "Gpt 4O Mini"
        assert m.provider == "openai"
        assert m.model_id == "openai/gpt-4o-mini"

    def test_underscores_and_hyphens_both_become_spaces(self):
        assert get_default_metadata("some_model-name", "p").display_name == "Some Model Name"

    def test_streaming_is_always_assumed(self):
        assert "streaming" in get_default_metadata("mystery", "p").capabilities

    @pytest.mark.parametrize("model_id", ["gpt-4", "claude-3-opus", "gemini-1.5", "llama-3-70b"])
    def test_known_families_get_function_calling(self, model_id):
        assert "function_calling" in get_default_metadata(model_id, "p").capabilities

    def test_an_unknown_family_does_not(self):
        assert "function_calling" not in get_default_metadata("mystery-1", "p").capabilities

    @pytest.mark.parametrize(
        "model_id", ["gpt-4o", "gemini-2-flash", "claude-3-5-sonnet", "claude-sonnet-4"]
    )
    def test_known_vision_families_get_vision(self, model_id):
        assert "vision" in get_default_metadata(model_id, "p").capabilities

    def test_an_unknown_family_does_not_get_vision(self):
        assert "vision" not in get_default_metadata("gpt-4", "p").capabilities

    @pytest.mark.parametrize(
        "model_id",
        ["gpt-4o-mini", "nano-1", "gemini-flash", "claude-haiku", "instant-1", "turbo-2"],
    )
    def test_small_models_are_fast(self, model_id):
        assert get_default_metadata(model_id, "p").speed == "fast"

    @pytest.mark.parametrize("model_id", ["claude-opus-4", "llama-large", "some-pro-model"])
    def test_big_models_are_slow(self, model_id):
        assert get_default_metadata(model_id, "p").speed == "slow"

    @pytest.mark.parametrize("model_id", ["gemini-pro", "gemini-ultra", "gemini-2.5-pro"])
    def test_every_gemini_is_called_fast_known_bug(self, model_id):
        """`"mini" in "gemini"` is True, so the `fast` arm swallows the whole family.

        This pins the behaviour as it actually ships, not as it should be. The
        substring checks are unanchored, so `gemini-2.5-pro` reports
        `speed="fast"` and `gemini-ultra` reports `tier="budget"`. Both reach the
        app's model picker. Fixing it means matching on token boundaries rather
        than substrings; this test should be inverted at the same time.
        """
        assert get_default_metadata(model_id, "p").speed == "fast"

    def test_gemini_ultra_is_called_budget_tier_known_bug(self):
        """Same unanchored-substring cause, in the tier arm."""
        assert get_default_metadata("gemini-ultra", "p").tier == "budget"

    def test_anything_else_is_standard_speed(self):
        assert get_default_metadata("mystery-1", "p").speed == "standard"

    @pytest.mark.parametrize(
        "model_id", ["claude-opus-4", "gemini-pro", "llama-large", "the-flagship"]
    )
    def test_big_models_are_flagship_tier(self, model_id):
        assert get_default_metadata(model_id, "p").tier == "flagship"

    @pytest.mark.parametrize(
        "model_id", ["gpt-4o-mini", "nano-1", "lite-1", "claude-haiku", "small-1", "budget-1"]
    )
    def test_small_models_are_budget_tier(self, model_id):
        assert get_default_metadata(model_id, "p").tier == "budget"

    def test_anything_else_is_standard_tier(self):
        assert get_default_metadata("mystery-1", "p").tier == "standard"

    def test_the_inference_is_case_insensitive(self):
        assert get_default_metadata("GPT-4O-MINI", "p").speed == "fast"
        assert "function_calling" in get_default_metadata("GPT-4", "p").capabilities

    def test_unknown_models_get_conservative_numeric_defaults(self):
        m = get_default_metadata("mystery-1", "p")
        assert m.context_window == 128000
        assert m.max_output == 8192
