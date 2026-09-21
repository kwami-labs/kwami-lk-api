"""`src.services.admin_reconciliation` — provider invoices vs the ledger.

This is what an operator opens when the numbers disagree with a bill. The four
provider pulls go over plain `urllib` (no httpx), so they are stubbed at
`_json_request`; the reconciliation maths is exercised directly.

The findings are the product: a provider cost delta, a line nobody can attribute
to a session, and a session billed for less than it cost. Each has a test.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from src.services import admin_reconciliation as ar
from src.services.admin_reconciliation import (
    SUPPORTED_PROVIDERS,
    ProviderUsageLine,
    _build_livekit_analytics_token,
    _datetime_to_iso,
    _fetch_rows,
    _filter_period_rows,
    _infer_internal_provider,
    _json_request,
    _normalize_provider,
    _normalize_zep_usage_payload,
    _parse_datetime,
    _provider_cost,
    _pull_livekit_usage,
    _pull_openai_costs,
    _pull_tavily_usage,
    _pull_zep_usage,
    _round_usd,
    _serialize_line,
    _summarize_imported_lines,
    _summarize_internal_rows,
    create_provider_usage_import,
    create_reconciliation_run,
    get_provider_usage_import,
    get_reconciliation_run,
    import_provider_usage_from_api,
    import_provider_usage_manual,
    insert_provider_usage_lines,
    list_provider_usage_imports,
    list_reconciliation_runs,
    normalize_manual_import_lines,
    pull_provider_usage,
    replace_reconciliation_findings,
    run_admin_reconciliation,
)

START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 3, 31, tzinfo=UTC)


@pytest.fixture
def json_requests(monkeypatch):
    """Replace the stdlib JSON GET with a scripted queue."""
    calls: list[tuple[str, dict, dict]] = []
    responses: list = []

    async def fake(url, *, headers=None, query=None):
        calls.append((url, headers or {}, query or {}))
        if not responses:
            return {}
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ar, "_json_request", fake)
    return type("Q", (), {"calls": calls, "responses": responses})()


# -- small helpers -----------------------------------------------------------


class TestNormalizeProvider:
    @pytest.mark.parametrize("provider", sorted(SUPPORTED_PROVIDERS))
    def test_every_supported_provider(self, provider):
        assert _normalize_provider(provider) == provider

    def test_it_is_case_and_space_insensitive(self):
        assert _normalize_provider("  OpenAI  ") == "openai"

    def test_an_unsupported_provider_is_refused(self):
        with pytest.raises(ValueError, match="Unsupported provider: stripe"):
            _normalize_provider("stripe")


class TestDatetimeHelpers:
    def test_iso_output_assumes_utc_for_naive_values(self):
        assert _datetime_to_iso(datetime(2026, 3, 1)).endswith("+00:00")

    def test_iso_output_preserves_an_offset(self):
        value = datetime(2026, 3, 1, tzinfo=UTC)
        assert _datetime_to_iso(value) == value.isoformat()

    def test_none_stays_none(self):
        assert _datetime_to_iso(None) is None

    @pytest.mark.parametrize("value", [None, ""])
    def test_parsing_an_empty_value(self, value):
        assert _parse_datetime(value) is None

    def test_parsing_a_blank_string(self):
        assert _parse_datetime("   ") is None

    def test_parsing_an_aware_datetime_passes_it_through(self):
        assert _parse_datetime(START) == START

    def test_parsing_a_naive_datetime_assumes_utc(self):
        assert _parse_datetime(datetime(2026, 3, 1)).tzinfo is UTC

    def test_parsing_a_unix_timestamp(self):
        assert _parse_datetime(0) == datetime(1970, 1, 1, tzinfo=UTC)

    def test_parsing_a_float_timestamp(self):
        assert _parse_datetime(0.0).year == 1970

    def test_parsing_a_zulu_string(self):
        assert _parse_datetime("2026-03-01T00:00:00Z") == START

    def test_parsing_an_offset_string(self):
        assert _parse_datetime("2026-03-01T00:00:00+00:00") == START

    def test_an_unsupported_type_is_refused(self):
        with pytest.raises(ValueError, match="Unsupported datetime value"):
            _parse_datetime(["not", "a", "date"])


class TestProviderCost:
    def test_the_raw_cost_wins(self):
        assert _provider_cost({"raw_cost_usd": 1.5, "estimated_cost_usd": 9.0}) == 1.5

    def test_the_estimate_is_the_fallback(self):
        assert _provider_cost({"estimated_cost_usd": 9.0}) == 9.0

    def test_neither_is_free(self):
        assert _provider_cost({}) == 0.0

    def test_an_explicit_zero_raw_cost_is_honoured(self):
        """Unlike the ledger's `or` fallback, this checks `is not None`."""
        assert _provider_cost({"raw_cost_usd": 0.0, "estimated_cost_usd": 9.0}) == 0.0


def test_round_usd_keeps_six_places():
    assert _round_usd(1.23456789) == 1.234568


class TestInferInternalProvider:
    def test_a_priced_model(self):
        from src.services.pricing import ALL_PRICING

        model_id, entry = next(iter(ALL_PRICING.items()))
        assert _infer_internal_provider(model_id) == entry.provider

    def test_a_namespaced_unknown_model(self):
        assert _infer_internal_provider("someprov/model") == "someprov"

    def test_a_bare_unknown_model(self):
        assert _infer_internal_provider("mystery") == "unknown"


class TestJsonRequest:
    """The provider pulls moved from `urllib.request.urlopen` to httpx.

    `urlopen` is synchronous, and these run from `async def` route handlers with a
    30s timeout, in a loop over paginated provider APIs -- one reconciliation run
    could hold the event loop for minutes. Stubbed at the transport with respx so
    the timeout, the status handling and the query encoding are all still covered.
    """

    URL = "https://provider.example/api"

    @pytest.mark.anyio
    @respx.mock
    async def test_a_http_error_status_becomes_a_runtime_error(self):
        respx.get(self.URL).mock(return_value=httpx.Response(500, text="upstream detail"))
        with pytest.raises(RuntimeError, match=r"Provider API request failed \(500\)"):
            await _json_request(self.URL)

    @pytest.mark.anyio
    @respx.mock
    async def test_the_upstream_body_is_truncated_into_the_message(self):
        respx.get(self.URL).mock(return_value=httpx.Response(502, text="x" * 900))
        with pytest.raises(RuntimeError) as excinfo:
            await _json_request(self.URL)
        assert len(str(excinfo.value)) < 600

    @pytest.mark.anyio
    @respx.mock
    async def test_a_transport_error_becomes_a_runtime_error(self):
        respx.get(self.URL).mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(RuntimeError, match="Provider API request failed"):
            await _json_request(self.URL)

    @pytest.mark.anyio
    @respx.mock
    async def test_a_successful_request_parses_json(self):
        respx.get(self.URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        assert await _json_request(self.URL) == {"ok": True}

    @pytest.mark.anyio
    @respx.mock
    async def test_the_query_is_encoded_and_nones_dropped(self):
        route = respx.get(self.URL).mock(return_value=httpx.Response(200, json={}))
        await _json_request(self.URL, query={"a": 1, "b": None, "c": ["x", "y"]})
        sent = str(route.calls.last.request.url)
        assert "a=1" in sent
        assert "b=" not in sent
        assert "c=x&c=y" in sent

    @pytest.mark.anyio
    @respx.mock
    async def test_the_headers_are_sent(self):
        route = respx.get(self.URL).mock(return_value=httpx.Response(200, json={}))
        await _json_request(self.URL, headers={"Authorization": "Bearer t"})
        assert route.calls.last.request.headers["authorization"] == "Bearer t"

    @pytest.mark.anyio
    async def test_a_non_https_url_is_refused(self):
        """`urlopen` would have followed a `file:` URL out of a mistyped setting."""
        with pytest.raises(RuntimeError, match="must be https"):
            await _json_request("http://provider.example/api")

    @pytest.mark.anyio
    async def test_a_file_url_is_refused(self):
        with pytest.raises(RuntimeError, match="must be https"):
            await _json_request("file:///etc/passwd")


class TestBuildLivekitAnalyticsToken:
    def test_a_configured_token_is_used_verbatim(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "livekit_analytics_token", "tok-123", raising=False)
        assert _build_livekit_analytics_token() == "tok-123"

    def test_otherwise_one_is_minted(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "livekit_analytics_token", None, raising=False)
        token = _build_livekit_analytics_token()
        assert token.count(".") == 2, "a JWT"


# -- provider pulls ----------------------------------------------------------


class TestPullOpenaiCosts:
    @pytest.mark.anyio
    async def test_an_unconfigured_key_is_refused(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "openai_admin_key", None, raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_ADMIN_KEY"):
            await _pull_openai_costs(START, END)

    @pytest.fixture(autouse=True)
    def _keyed(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "openai_admin_key", "sk-admin", raising=False)

    @pytest.mark.anyio
    async def test_a_single_page_of_costs(self, json_requests):
        json_requests.responses.append(
            {
                "data": [
                    {
                        "start_time": 1772409600,
                        "end_time": 1772496000,
                        "results": [
                            {
                                "amount": {"value": 1.25, "currency": "USD"},
                                "project_id": "proj_1",
                                "line_item": "gpt-4o",
                            },
                        ],
                    }
                ],
                "has_more": False,
            }
        )
        result = await _pull_openai_costs(START, END)
        assert result["source_label"] == "openai.organization.costs"
        assert result["summary"] == {"pages": 1, "lines_count": 1}
        (line,) = result["lines"]
        assert line.provider == "openai"
        assert line.service == "gpt-4o"
        assert line.raw_cost_usd == 1.25
        assert line.currency == "usd"
        assert line.resource_id == "proj_1"

    @pytest.mark.anyio
    async def test_pagination_follows_next_page(self, json_requests):
        json_requests.responses.extend(
            [
                {"data": [], "has_more": True, "next_page": "p2"},
                {"data": [], "has_more": False},
            ]
        )
        result = await _pull_openai_costs(START, END)
        assert result["summary"]["pages"] == 2
        assert json_requests.calls[1][2]["page"] == "p2"

    @pytest.mark.anyio
    async def test_has_more_without_a_next_page_stops(self, json_requests):
        json_requests.responses.append({"data": [], "has_more": True})
        assert (await _pull_openai_costs(START, END))["summary"]["pages"] == 1

    @pytest.mark.anyio
    async def test_a_result_with_no_amount_is_skipped(self, json_requests):
        json_requests.responses.append(
            {"data": [{"results": [{"amount": {}}, {"project_id": "p"}]}], "has_more": False}
        )
        assert (await _pull_openai_costs(START, END))["lines"] == []

    @pytest.mark.anyio
    async def test_the_singular_result_key_is_accepted(self, json_requests):
        json_requests.responses.append(
            {"data": [{"result": [{"amount": {"value": 1.0}}]}], "has_more": False}
        )
        assert len((await _pull_openai_costs(START, END))["lines"]) == 1

    @pytest.mark.anyio
    async def test_a_missing_line_item_defaults(self, json_requests):
        json_requests.responses.append(
            {"data": [{"results": [{"amount": {"value": 1.0}}]}], "has_more": False}
        )
        assert (await _pull_openai_costs(START, END))["lines"][0].service == "openai_api"

    @pytest.mark.anyio
    async def test_project_ids_are_passed_through(self, json_requests):
        json_requests.responses.append({"data": [], "has_more": False})
        await _pull_openai_costs(START, END, project_ids=["proj_1"])
        assert json_requests.calls[0][2]["project_ids"] == ["proj_1"]


class TestPullTavilyUsage:
    @pytest.mark.anyio
    async def test_an_unconfigured_key_is_refused(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "tavily_api_key", None, raising=False)
        with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
            await _pull_tavily_usage(START, END)

    @pytest.fixture(autouse=True)
    def _keyed(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "tavily_api_key", "tvly-1", raising=False)
        monkeypatch.setattr(ar.settings, "tavily_project_id", None, raising=False)
        monkeypatch.setattr(
            ar.settings, "reconciliation_tavily_cost_per_credit_usd", 0.008, raising=False
        )

    @pytest.mark.anyio
    async def test_each_used_service_becomes_a_line(self, json_requests):
        json_requests.responses.append(
            {
                "account": {
                    "search_usage": 100,
                    "extract_usage": 50,
                    "crawl_usage": 0,
                    "current_plan": "pro",
                    "plan_usage": 150,
                    "plan_limit": 1000,
                }
            }
        )
        result = await _pull_tavily_usage(START, END)
        assert {line.service for line in result["lines"]} == {"search", "extract"}
        search = next(line for line in result["lines"] if line.service == "search")
        assert search.usage_quantity == 100
        assert search.raw_cost_usd == pytest.approx(0.8)

    @pytest.mark.anyio
    async def test_a_zero_or_absent_usage_is_skipped(self, json_requests):
        json_requests.responses.append({"account": {"search_usage": 0}})
        assert (await _pull_tavily_usage(START, END))["lines"] == []

    @pytest.mark.anyio
    async def test_an_empty_payload(self, json_requests):
        json_requests.responses.append({})
        result = await _pull_tavily_usage(START, END)
        assert result["lines"] == []
        assert result["summary"]["lines_count"] == 0

    @pytest.mark.anyio
    async def test_an_explicit_project_id_is_sent_as_a_header(self, json_requests):
        json_requests.responses.append({"account": {}})
        await _pull_tavily_usage(START, END, project_id="proj-x")
        assert json_requests.calls[0][1]["X-Project-ID"] == "proj-x"

    @pytest.mark.anyio
    async def test_the_configured_project_id_is_the_fallback(self, monkeypatch, json_requests):
        monkeypatch.setattr(ar.settings, "tavily_project_id", "proj-cfg", raising=False)
        json_requests.responses.append({"account": {}})
        await _pull_tavily_usage(START, END)
        assert json_requests.calls[0][1]["X-Project-ID"] == "proj-cfg"

    @pytest.mark.anyio
    async def test_no_project_id_sends_no_header(self, json_requests):
        json_requests.responses.append({"account": {}})
        await _pull_tavily_usage(START, END)
        assert "X-Project-ID" not in json_requests.calls[0][1]


class TestPullLivekitUsage:
    @pytest.mark.anyio
    async def test_an_unconfigured_project_is_refused(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "livekit_cloud_project_id", None, raising=False)
        with pytest.raises(RuntimeError, match="LIVEKIT_CLOUD_PROJECT_ID"):
            await _pull_livekit_usage(START, END)

    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "livekit_cloud_project_id", "proj_1", raising=False)
        monkeypatch.setattr(ar.settings, "livekit_analytics_token", "tok", raising=False)
        monkeypatch.setattr(
            ar.settings, "reconciliation_livekit_connection_minute_usd", 0.01, raising=False
        )
        monkeypatch.setattr(
            ar.settings, "reconciliation_livekit_bandwidth_gb_usd", 0.1, raising=False
        )

    @pytest.mark.anyio
    async def test_sessions_become_estimated_lines(self, json_requests):
        json_requests.responses.extend(
            [
                {"sessions": [{"sessionId": "S1", "roomName": "room-1"}]},
                {
                    "connectionMinutes": 10,
                    "bandwidth": 2_000_000_000,
                    "roomName": "room-1",
                    "numParticipants": 2,
                    "startTime": "2026-03-01T00:00:00Z",
                    "endTime": "2026-03-01T00:10:00Z",
                },
            ]
        )
        result = await _pull_livekit_usage(START, END, limit=100)
        (line,) = result["lines"]
        assert line.session_id == "S1"
        assert line.usage_quantity == 10
        # 10 min * 0.01 + 2 GB * 0.1
        assert line.estimated_cost_usd == pytest.approx(0.1 + 0.2)
        assert line.metadata["bandwidth_gb"] == 2.0

    @pytest.mark.anyio
    async def test_bandwidth_falls_back_to_the_session_totals(self, json_requests):
        json_requests.responses.extend(
            [
                {
                    "sessions": [
                        {
                            "sessionId": "S1",
                            "bandwidthIn": 1_000_000_000,
                            "bandwidthOut": 1_000_000_000,
                        }
                    ]
                },
                {},
            ]
        )
        (line,) = (await _pull_livekit_usage(START, END))["lines"]
        assert line.metadata["bandwidth_gb"] == 2.0

    @pytest.mark.anyio
    async def test_a_session_without_an_id_skips_the_detail_lookup(self, json_requests):
        json_requests.responses.append({"sessions": [{"roomName": "room-1"}]})
        result = await _pull_livekit_usage(START, END)
        assert len(result["lines"]) == 1
        assert len(json_requests.calls) == 1

    @pytest.mark.anyio
    async def test_a_failing_detail_lookup_does_not_lose_the_session(self, json_requests, caplog):
        json_requests.responses.extend(
            [
                {"sessions": [{"sessionId": "S1"}]},
                RuntimeError("detail lookup failed"),
            ]
        )
        with caplog.at_level("WARNING", logger="kwami-api.admin-reconciliation"):
            result = await _pull_livekit_usage(START, END)
        assert len(result["lines"]) == 1
        assert "detail lookup failed" in caplog.text

    @pytest.mark.anyio
    async def test_pagination_stops_on_a_short_page(self, json_requests):
        json_requests.responses.extend(
            [
                {"sessions": [{"sessionId": "S1"}, {"sessionId": "S2"}]},
                {},
                {},
            ]
        )
        result = await _pull_livekit_usage(START, END, limit=2)
        assert result["summary"]["pages_fetched"] >= 1

    @pytest.mark.anyio
    async def test_an_empty_project(self, json_requests):
        json_requests.responses.append({"sessions": []})
        result = await _pull_livekit_usage(START, END)
        assert result["lines"] == []
        assert result["summary"]["sessions_count"] == 0


class TestPullZepUsage:
    @pytest.mark.anyio
    async def test_an_unconfigured_key_is_refused(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "zep_api_key", None, raising=False)
        with pytest.raises(RuntimeError, match="ZEP_API_KEY"):
            await _pull_zep_usage(START, END)

    @pytest.fixture(autouse=True)
    def _keyed(self, monkeypatch):
        monkeypatch.setattr(ar.settings, "zep_api_key", "z-1", raising=False)
        monkeypatch.setattr(ar.settings, "zep_usage_api_url", None, raising=False)

    @pytest.mark.anyio
    async def test_without_a_usage_url_it_warns_and_returns_no_lines(self, json_requests):
        """Zep has no standard billing endpoint; say so rather than pretend."""
        json_requests.responses.append({"project": {"name": "kwami"}})
        result = await _pull_zep_usage(START, END)
        assert result["source_label"] == "zep.project_info"
        assert result["lines"] == []
        assert "does not expose a standard usage billing endpoint" in result["summary"]["warning"]

    @pytest.mark.anyio
    async def test_with_a_usage_url_the_lines_are_normalized(self, monkeypatch, json_requests):
        monkeypatch.setattr(
            ar.settings, "zep_usage_api_url", "https://zep.test/usage", raising=False
        )
        json_requests.responses.extend(
            [
                {"project": {}},
                {"data": [{"service": "search", "count": 5, "cost_usd": 0.5}]},
            ]
        )
        result = await _pull_zep_usage(START, END)
        assert result["source_label"] == "zep.usage_api"
        assert result["summary"]["lines_count"] == 1


class TestNormalizeZepUsagePayload:
    def test_a_data_list(self):
        lines = _normalize_zep_usage_payload({"data": [{"service": "s"}]}, START, END)
        assert len(lines) == 1

    def test_a_usage_list(self):
        assert len(_normalize_zep_usage_payload({"usage": [{"service": "s"}]}, START, END)) == 1

    def test_a_bare_dict_is_one_line(self):
        assert len(_normalize_zep_usage_payload({"service": "s"}, START, END)) == 1

    def test_a_bare_list(self):
        assert len(_normalize_zep_usage_payload([{"service": "s"}], START, END)) == 1

    def test_anything_else_yields_nothing(self):
        assert _normalize_zep_usage_payload("nonsense", START, END) == []
        assert _normalize_zep_usage_payload(None, START, END) == []

    def test_non_dict_items_are_skipped(self):
        assert _normalize_zep_usage_payload([1, "x", {"service": "s"}], START, END) != []
        assert len(_normalize_zep_usage_payload([1, "x", {"service": "s"}], START, END)) == 1

    def test_every_alias_is_read(self):
        (line,) = _normalize_zep_usage_payload(
            [
                {
                    "endpoint": "graph.search",
                    "unit": "call",
                    "count": 3,
                    "estimated_cost_usd": 0.3,
                    "thread_id": "t1",
                    "user_id": "u1",
                    "currency": "USD",
                    "resource_id": "r1",
                }
            ],
            START,
            END,
        )
        assert line.service == "graph.search"
        assert line.usage_unit == "call"
        assert line.usage_quantity == 3
        assert line.estimated_cost_usd == 0.3
        assert line.session_id == "t1"
        assert line.currency == "usd"
        assert line.resource_id == "r1"

    def test_the_period_is_the_timestamp_fallback(self):
        (line,) = _normalize_zep_usage_payload([{"service": "s"}], START, END)
        assert line.started_at == START
        assert line.ended_at == END

    def test_unknown_keys_land_in_metadata(self):
        (line,) = _normalize_zep_usage_payload([{"service": "s", "custom_field": "x"}], START, END)
        assert line.metadata == {"custom_field": "x"}

    def test_the_defaults(self):
        (line,) = _normalize_zep_usage_payload([{}], START, END)
        assert line.service == "usage"
        assert line.usage_unit == "request"
        assert line.usage_quantity == 1
        assert line.raw_cost_usd is None


class TestPullProviderUsage:
    @pytest.mark.anyio
    @pytest.mark.parametrize("provider", sorted(SUPPORTED_PROVIDERS))
    async def test_each_provider_routes_to_its_puller(self, monkeypatch, provider):
        seen: list[str] = []
        for name in (
            "_pull_openai_costs",
            "_pull_tavily_usage",
            "_pull_livekit_usage",
            "_pull_zep_usage",
        ):

            async def _stub(*_a, _n=name, **_k):
                seen.append(_n)
                return {"lines": []}

            monkeypatch.setattr(ar, name, _stub)
        await pull_provider_usage(provider, START, END)
        assert len(seen) == 1

    @pytest.mark.anyio
    async def test_options_reach_the_puller(self, monkeypatch):
        seen: dict = {}

        async def _stub(_start, _end, *, limit):
            seen.update(limit=limit)
            return {"lines": []}

        monkeypatch.setattr(ar, "_pull_livekit_usage", _stub)
        await pull_provider_usage("livekit", START, END, options={"limit": 7})
        assert seen["limit"] == 7

    @pytest.mark.anyio
    async def test_an_absent_limit_defaults(self, monkeypatch):
        seen: dict = {}

        async def _stub(_start, _end, *, limit):
            seen.update(limit=limit)
            return {"lines": []}

        monkeypatch.setattr(ar, "_pull_livekit_usage", _stub)
        await pull_provider_usage("livekit", START, END, options={})
        assert seen["limit"] == 100

    @pytest.mark.anyio
    async def test_an_unsupported_provider_is_refused(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            await pull_provider_usage("stripe", START, END)

    @pytest.mark.anyio
    async def test_a_supported_provider_with_no_case_falls_through_to_a_clear_error(
        self, monkeypatch
    ):
        """The guard after the `match`, which `_normalize_provider` normally shadows.

        Adding a provider to SUPPORTED_PROVIDERS without adding a `case` for it
        gets past normalization and reaches here. The alternative -- falling off
        the end of the function and returning None -- would fail at the caller's
        `pulled["lines"]` with a KeyError instead.
        """
        monkeypatch.setattr(ar, "SUPPORTED_PROVIDERS", SUPPORTED_PROVIDERS | {"newprovider"})
        with pytest.raises(ValueError, match="Unsupported provider: newprovider"):
            await pull_provider_usage("newprovider", START, END)


class TestNormalizeManualImportLines:
    def test_the_defaults(self):
        (line,) = normalize_manual_import_lines("openai", [{}])
        assert line.provider == "openai"
        assert line.service == "manual"
        assert line.usage_unit == "unit"
        assert line.usage_quantity == 0.0
        assert line.raw_cost_usd is None
        assert line.currency == "usd"
        assert line.metadata == {}

    def test_every_field_is_carried(self):
        (line,) = normalize_manual_import_lines(
            "tavily",
            [
                {
                    "service": "search",
                    "usage_unit": "credits",
                    "usage_quantity": 12,
                    "raw_cost_usd": 0.096,
                    "estimated_cost_usd": 0.1,
                    "currency": "EUR",
                    "resource_id": "r1",
                    "session_id": "s1",
                    "user_id": "u1",
                    "started_at": "2026-03-01T00:00:00Z",
                    "ended_at": "2026-03-02T00:00:00Z",
                    "external_reference": "inv-1",
                    "metadata": {"source": "csv"},
                }
            ],
        )
        assert line.currency == "eur"
        assert line.session_id == "s1"
        assert line.started_at == START
        assert line.external_reference == "inv-1"
        assert line.metadata == {"source": "csv"}

    def test_the_raw_line_defaults_to_the_input(self):
        (line,) = normalize_manual_import_lines("openai", [{"service": "x"}])
        assert line.raw_line == {"service": "x"}

    def test_an_explicit_raw_line_wins(self):
        (line,) = normalize_manual_import_lines(
            "openai", [{"service": "x", "raw_line": {"original": 1}}]
        )
        assert line.raw_line == {"original": 1}

    def test_an_unsupported_provider_is_refused(self):
        with pytest.raises(ValueError):
            normalize_manual_import_lines("stripe", [])


def test_serialize_line_renders_datetimes_as_iso():
    line = ProviderUsageLine(
        provider="openai", service="api", usage_unit="usd", started_at=START, ended_at=END
    )
    row = _serialize_line(line, "imp-1")
    assert row["import_id"] == "imp-1"
    assert row["started_at"] == START.isoformat()
    assert row["ended_at"] == END.isoformat()


class TestFilterPeriodRows:
    ROWS = [
        {"started_at": "2026-02-01T00:00:00Z"},
        {"started_at": "2026-03-15T00:00:00Z"},
        {"started_at": "2026-04-01T00:00:00Z"},
        {"started_at": None},
    ]

    def test_no_bounds_keeps_everything(self):
        assert (
            len(
                _filter_period_rows(
                    self.ROWS, started_at_key="started_at", period_start=None, period_end=None
                )
            )
            == 4
        )

    def test_a_start_bound(self):
        rows = _filter_period_rows(
            self.ROWS, started_at_key="started_at", period_start=START, period_end=None
        )
        assert len(rows) == 3, "the February row is dropped, the null-time row is kept"

    def test_an_end_bound(self):
        rows = _filter_period_rows(
            self.ROWS, started_at_key="started_at", period_start=None, period_end=END
        )
        assert len(rows) == 3, "the April row is dropped"

    def test_both_bounds(self):
        rows = _filter_period_rows(
            self.ROWS, started_at_key="started_at", period_start=START, period_end=END
        )
        assert len(rows) == 2, "March plus the null-time row"

    def test_rows_without_a_timestamp_are_always_kept(self):
        rows = _filter_period_rows(
            [{"started_at": None}],
            started_at_key="started_at",
            period_start=START,
            period_end=END,
        )
        assert len(rows) == 1


class TestSummarizeInternalRows:
    def test_an_empty_ledger(self):
        providers, meta = _summarize_internal_rows([])
        assert providers == []
        assert meta["findings"] == []

    def test_costs_are_grouped_by_provider(self):
        providers, _ = _summarize_internal_rows(
            [
                {
                    "model_id": "openai/gpt-4o",
                    "provider_cost_usd": 1.0,
                    "billed_cost_usd": 2.0,
                    "session_id": "s1",
                    "model_type": "llm",
                },
                {
                    "model_id": "openai/gpt-4o",
                    "provider_cost_usd": 2.0,
                    "billed_cost_usd": 4.0,
                    "session_id": "s2",
                    "model_type": "llm",
                },
            ]
        )
        (row,) = providers
        assert row["internal_provider_cost_usd"] == 3.0
        assert row["usage_rows"] == 2
        assert row["sessions_count"] == 2

    def test_providers_are_ranked_by_cost(self):
        providers, _ = _summarize_internal_rows(
            [
                {"model_id": "openai/gpt-4o", "provider_cost_usd": 1.0, "session_id": "s"},
                {"model_id": "anthropic/claude", "provider_cost_usd": 5.0, "session_id": "s"},
            ]
        )
        costs = [p["internal_provider_cost_usd"] for p in providers]
        assert costs == sorted(costs, reverse=True)

    def test_the_legacy_cost_column_is_a_fallback(self):
        providers, _ = _summarize_internal_rows(
            [{"model_id": "openai/gpt-4o", "cost_usd": 3.0, "session_id": "s"}]
        )
        assert providers[0]["internal_provider_cost_usd"] == 3.0

    def test_fallback_pricing_is_a_finding(self):
        _, meta = _summarize_internal_rows(
            [
                {
                    "model_id": "openai/gpt-4o",
                    "provider_cost_usd": 1.0,
                    "session_id": "s",
                    "pricing_source": "fallback",
                },
            ]
        )
        assert any(f["finding_type"] == "fallback_pricing" for f in meta["findings"])

    def test_a_zero_provider_cost_is_a_finding(self):
        _, meta = _summarize_internal_rows(
            [{"model_id": "openai/gpt-4o", "provider_cost_usd": 0.0, "session_id": "s"}]
        )
        assert any(f["finding_type"] == "zero_provider_cost" for f in meta["findings"])

    def test_a_row_without_a_session_is_grouped_as_unknown(self):
        _, meta = _summarize_internal_rows([{"model_id": "m", "provider_cost_usd": 1.0}])
        assert "unknown" in meta["by_session"]


class TestSummarizeImportedLines:
    def test_an_empty_import(self):
        providers, meta = _summarize_imported_lines([])
        assert providers == []
        assert meta["matched_session_ids"] == set()

    def test_lines_are_grouped_by_provider(self):
        providers, meta = _summarize_imported_lines(
            [
                {"provider": "openai", "service": "api", "raw_cost_usd": 1.0, "session_id": "s1"},
                {"provider": "openai", "service": "embed", "raw_cost_usd": 2.0},
            ]
        )
        (row,) = providers
        assert row["imported_cost_usd"] == 3.0
        assert row["imported_raw_cost_usd"] == 3.0
        assert row["usage_lines"] == 2
        assert row["services"] == ["api", "embed"]
        assert meta["matched_session_ids"] == {"s1"}

    def test_estimated_costs_are_tracked_separately(self):
        providers, _ = _summarize_imported_lines(
            [{"provider": "livekit", "service": "s", "estimated_cost_usd": 4.0}]
        )
        assert providers[0]["imported_estimated_cost_usd"] == 4.0
        assert providers[0]["imported_raw_cost_usd"] == 0.0

    def test_an_external_reference_counts_toward_sessions(self):
        providers, meta = _summarize_imported_lines(
            [{"provider": "livekit", "service": "s", "external_reference": "room-1"}]
        )
        assert providers[0]["sessions_count"] == 1
        assert meta["matched_session_ids"] == set(), "only session_id is a match"

    def test_providers_are_ranked_by_cost(self):
        providers, _ = _summarize_imported_lines(
            [
                {"provider": "a", "service": "s", "raw_cost_usd": 1.0},
                {"provider": "b", "service": "s", "raw_cost_usd": 5.0},
            ]
        )
        assert providers[0]["provider"] == "b"


# -- persistence and the run itself ------------------------------------------


def _empty_insert_client():
    class _T:
        def insert(self, payload):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def order(self, *a, **k):
            return self

        def range(self, *a, **k):
            return self

        async def execute(self):
            return type("R", (), {"data": []})()

    return type("C", (), {"table": staticmethod(lambda n: _T())})()


class TestFetchRows:
    @pytest.mark.anyio
    async def test_it_pages_until_a_short_batch(self, monkeypatch, fake_supabase):
        pages = [[{"id": i} for i in range(3)], [{"id": 99}]]
        seen: list[tuple[int, int]] = []

        class _T:
            def select(self, *a, **k):
                return self

            def order(self, *a, **k):
                return self

            def eq(self, *a, **k):
                return self

            def gte(self, *a, **k):
                return self

            def lte(self, *a, **k):
                return self

            def in_(self, *a, **k):
                return self

            def range(self, start, end):
                seen.append((start, end))
                return self

            async def execute(self):
                return type("R", (), {"data": pages.pop(0) if pages else []})()

        monkeypatch.setattr(
            ar,
            "get_supabase_admin",
            lambda: type("C", (), {"table": staticmethod(lambda n: _T())})(),
        )
        rows = await _fetch_rows("t", batch_size=3)
        assert len(rows) == 4
        assert seen == [(0, 2), (3, 5)]

    @pytest.mark.anyio
    async def test_every_filter_operator_is_applied(self, monkeypatch, fake_supabase):
        applied: list[tuple[str, str, object]] = []

        class _T:
            def select(self, *a, **k):
                return self

            def order(self, *a, **k):
                return self

            def eq(self, c, v):
                applied.append(("eq", c, v))
                return self

            def gte(self, c, v):
                applied.append(("gte", c, v))
                return self

            def lte(self, c, v):
                applied.append(("lte", c, v))
                return self

            def in_(self, c, v):
                applied.append(("in", c, v))
                return self

            def range(self, *a):
                return self

            async def execute(self):
                return type("R", (), {"data": []})()

        monkeypatch.setattr(
            ar,
            "get_supabase_admin",
            lambda: type("C", (), {"table": staticmethod(lambda n: _T())})(),
        )
        await _fetch_rows(
            "t",
            filters=[
                ("eq", "a", 1),
                ("gte", "b", 2),
                ("lte", "c", 3),
                ("in", "d", [4]),
                ("eq", "skip", None),
                ("unknown", "e", 5),
            ],
        )
        assert applied == [("eq", "a", 1), ("gte", "b", 2), ("lte", "c", 3), ("in", "d", [4])]


class TestImportLifecycle:
    @pytest.mark.anyio
    async def test_creating_an_import(self, fake_supabase):
        import_id = await create_provider_usage_import(
            provider="OpenAI",
            import_mode="manual",
            source_label="csv",
            invoice_period_start=START,
            invoice_period_end=END,
            external_reference="inv-1",
            imported_by="admin@example.com",
        )
        (row,) = fake_supabase.db.rows("provider_usage_imports")
        assert str(row["id"]) == str(import_id)
        assert row["provider"] == "openai"
        assert row["status"] == "pending"
        assert row["currency"] == "usd"

    @pytest.mark.anyio
    async def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ar, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create provider usage import"):
            await create_provider_usage_import(
                provider="openai",
                import_mode="manual",
                source_label=None,
                invoice_period_start=None,
                invoice_period_end=None,
            )

    @pytest.mark.anyio
    async def test_inserting_no_lines_is_a_no_op(self, monkeypatch, fake_supabase):
        def explode():
            raise AssertionError("must not reach the database")

        monkeypatch.setattr(ar, "get_supabase_admin", explode)
        assert await insert_provider_usage_lines("imp-1", []) is None

    @pytest.mark.anyio
    async def test_a_manual_import_records_lines_and_a_summary(self, fake_supabase):
        result = await import_provider_usage_manual(
            provider="tavily",
            source_label=None,
            invoice_period_start=START,
            invoice_period_end=END,
            lines=[
                {"service": "search", "raw_cost_usd": 1.5},
                {"service": "extract", "raw_cost_usd": 0.5},
            ],
        )
        assert result["summary"] == {"lines_count": 2, "raw_cost_usd": 2.0}
        (row,) = fake_supabase.db.rows("provider_usage_imports")
        assert row["status"] == "completed"
        assert row["source_label"] == "manual_import", "the default label"
        assert len(fake_supabase.db.rows("provider_usage_lines")) == 2

    @pytest.mark.anyio
    async def test_a_manual_import_failure_marks_the_import_failed(
        self, monkeypatch, fake_supabase
    ):
        async def boom(import_id, lines):
            raise RuntimeError("insert failed")

        monkeypatch.setattr(ar, "insert_provider_usage_lines", boom)
        with pytest.raises(RuntimeError, match="insert failed"):
            await import_provider_usage_manual(
                provider="tavily",
                source_label=None,
                invoice_period_start=None,
                invoice_period_end=None,
                lines=[{"service": "search"}],
            )
        (row,) = fake_supabase.db.rows("provider_usage_imports")
        assert row["status"] == "failed"
        assert "insert failed" in row["error"]

    @pytest.mark.anyio
    async def test_an_api_pull_records_lines(self, monkeypatch, fake_supabase):
        async def _pull_stub(*a, **k):
            return {
                "source_label": "openai.organization.costs",
                "summary": {"pages": 1},
                "raw_payload": {"pages": []},
                "lines": [
                    ProviderUsageLine(
                        provider="openai", service="api", usage_unit="usd", raw_cost_usd=1.0
                    )
                ],
            }

        monkeypatch.setattr(ar, "pull_provider_usage", _pull_stub)
        result = await import_provider_usage_from_api(
            provider="openai",
            invoice_period_start=START,
            invoice_period_end=END,
        )
        assert result["summary"]["lines_count"] == 1
        assert fake_supabase.db.rows("provider_usage_imports")[0]["status"] == "completed"

    @pytest.mark.anyio
    async def test_a_zep_pull_with_no_lines_is_partial(self, monkeypatch, fake_supabase):
        """Zep has no billing endpoint, so an empty pull is expected, not a failure."""

        async def _pull_stub(*a, **k):
            return {
                "source_label": "zep.project_info",
                "summary": {},
                "raw_payload": {},
                "lines": [],
            }

        monkeypatch.setattr(ar, "pull_provider_usage", _pull_stub)
        await import_provider_usage_from_api(
            provider="zep", invoice_period_start=START, invoice_period_end=END
        )
        assert fake_supabase.db.rows("provider_usage_imports")[0]["status"] == "partial"

    @pytest.mark.anyio
    async def test_another_provider_with_no_lines_is_still_completed(
        self, monkeypatch, fake_supabase
    ):
        async def _pull_stub(*a, **k):
            return {"source_label": "x", "summary": {}, "raw_payload": {}, "lines": []}

        monkeypatch.setattr(ar, "pull_provider_usage", _pull_stub)
        await import_provider_usage_from_api(
            provider="openai", invoice_period_start=START, invoice_period_end=END
        )
        assert fake_supabase.db.rows("provider_usage_imports")[0]["status"] == "completed"

    @pytest.mark.anyio
    async def test_an_api_pull_failure_marks_the_import_failed(self, monkeypatch, fake_supabase):
        def boom(*a, **k):
            raise RuntimeError("provider API down")

        monkeypatch.setattr(ar, "pull_provider_usage", boom)
        with pytest.raises(RuntimeError, match="provider API down"):
            await import_provider_usage_from_api(
                provider="openai", invoice_period_start=START, invoice_period_end=END
            )
        assert fake_supabase.db.rows("provider_usage_imports")[0]["status"] == "failed"

    @pytest.mark.anyio
    async def test_listing_imports(self, fake_supabase):
        for provider in ("openai", "tavily"):
            await create_provider_usage_import(
                provider=provider,
                import_mode="manual",
                source_label=None,
                invoice_period_start=None,
                invoice_period_end=None,
            )
        assert len(await list_provider_usage_imports()) == 2

    @pytest.mark.anyio
    async def test_listing_imports_filtered_by_provider(self, fake_supabase):
        for provider in ("openai", "tavily"):
            await create_provider_usage_import(
                provider=provider,
                import_mode="manual",
                source_label=None,
                invoice_period_start=None,
                invoice_period_end=None,
            )
        rows = await list_provider_usage_imports(provider="OpenAI")
        assert [r["provider"] for r in rows] == ["openai"]

    @pytest.mark.anyio
    async def test_listing_imports_respects_the_limit(self, fake_supabase):
        for _ in range(3):
            await create_provider_usage_import(
                provider="openai",
                import_mode="manual",
                source_label=None,
                invoice_period_start=None,
                invoice_period_end=None,
            )
        assert len(await list_provider_usage_imports(limit=2)) == 2

    @pytest.mark.anyio
    async def test_getting_an_import_returns_its_lines(self, fake_supabase):
        result = await import_provider_usage_manual(
            provider="tavily",
            source_label=None,
            invoice_period_start=None,
            invoice_period_end=None,
            lines=[{"service": "search"}],
        )
        detail = await get_provider_usage_import(result["import_id"])
        assert detail["import"]["provider"] == "tavily"
        assert len(detail["lines"]) == 1

    @pytest.mark.anyio
    async def test_getting_an_unknown_import_is_not_found(self, fake_supabase):
        with pytest.raises(ValueError, match="Provider import not found"):
            await get_provider_usage_import("00000000-0000-0000-0000-000000000000")


class TestRunLifecycle:
    @pytest.mark.anyio
    async def test_creating_a_run(self, fake_supabase):
        run_id = await create_reconciliation_run(
            trigger_mode="manual",
            provider_filters=["openai"],
            import_ids=["imp-1"],
            period_start=START,
            period_end=END,
            created_by="admin@example.com",
        )
        (row,) = fake_supabase.db.rows("provider_reconciliation_runs")
        assert str(row["id"]) == str(run_id)
        assert row["status"] == "pending"

    @pytest.mark.anyio
    async def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ar, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create reconciliation run"):
            await create_reconciliation_run(
                trigger_mode="manual",
                provider_filters=[],
                import_ids=[],
                period_start=None,
                period_end=None,
                created_by=None,
            )

    @pytest.mark.anyio
    async def test_replacing_findings_clears_the_old_ones(self, fake_supabase):
        run_id = await create_reconciliation_run(
            trigger_mode="manual",
            provider_filters=[],
            import_ids=[],
            period_start=None,
            period_end=None,
            created_by=None,
        )
        finding = {"severity": "warning", "finding_type": "x", "provider": "openai"}
        await replace_reconciliation_findings(str(run_id), [finding, finding])
        assert len(fake_supabase.db.rows("provider_reconciliation_findings")) == 2
        await replace_reconciliation_findings(str(run_id), [finding])
        assert len(fake_supabase.db.rows("provider_reconciliation_findings")) == 1

    @pytest.mark.anyio
    async def test_replacing_with_nothing_just_clears(self, fake_supabase):
        run_id = await create_reconciliation_run(
            trigger_mode="manual",
            provider_filters=[],
            import_ids=[],
            period_start=None,
            period_end=None,
            created_by=None,
        )
        await replace_reconciliation_findings(
            str(run_id), [{"severity": "warning", "finding_type": "x"}]
        )
        await replace_reconciliation_findings(str(run_id), [])
        assert fake_supabase.db.rows("provider_reconciliation_findings") == []

    @pytest.mark.anyio
    async def test_listing_runs(self, fake_supabase):
        for _ in range(3):
            await create_reconciliation_run(
                trigger_mode="manual",
                provider_filters=[],
                import_ids=[],
                period_start=None,
                period_end=None,
                created_by=None,
            )
        assert len(await list_reconciliation_runs()) == 3
        assert len(await list_reconciliation_runs(limit=2)) == 2

    @pytest.mark.anyio
    async def test_getting_a_run_returns_its_findings(self, fake_supabase):
        run_id = await create_reconciliation_run(
            trigger_mode="manual",
            provider_filters=[],
            import_ids=[],
            period_start=None,
            period_end=None,
            created_by=None,
        )
        await replace_reconciliation_findings(
            str(run_id), [{"severity": "critical", "finding_type": "x"}]
        )
        detail = await get_reconciliation_run(str(run_id))
        assert detail["run"]["status"] == "pending"
        assert len(detail["findings"]) == 1

    @pytest.mark.anyio
    async def test_getting_an_unknown_run_is_not_found(self, fake_supabase):
        with pytest.raises(ValueError, match="Reconciliation run not found"):
            await get_reconciliation_run("00000000-0000-0000-0000-000000000000")


class TestRunAdminReconciliation:
    def _ledger(self, fake_supabase, **overrides):
        return fake_supabase.db.seed(
            "credit_usage_logs",
            {
                "user_id": "u1",
                "session_id": "room-1",
                "model_id": "openai/gpt-4o",
                "model_type": "llm",
                "units_used": 1000,
                "provider_cost_usd": 1.0,
                "billed_cost_usd": 2.0,
                "credits_charged": 2000,
                "created_at": "2026-03-15T00:00:00+00:00",
                **overrides,
            },
        )[0]

    def _line(self, fake_supabase, **overrides):
        return fake_supabase.db.seed(
            "provider_usage_lines",
            {
                "import_id": "imp-1",
                "provider": "openai",
                "service": "api",
                "usage_unit": "usd",
                "usage_quantity": 1.0,
                "raw_cost_usd": 1.0,
                "currency": "usd",
                "session_id": "room-1",
                "started_at": "2026-03-15T00:00:00+00:00",
                **overrides,
            },
        )[0]

    @pytest.mark.anyio
    async def test_an_empty_run_completes(self, fake_supabase):
        result = await run_admin_reconciliation()
        assert result["summary"]["counts"] == {
            "imported_usage_lines": 0,
            "internal_usage_rows": 0,
            "findings": 0,
            "matched_import_session_ids": 0,
        }
        assert fake_supabase.db.rows("provider_reconciliation_runs")[0]["status"] == "completed"

    @pytest.mark.anyio
    async def test_matching_costs_produce_no_delta_finding(self, fake_supabase):
        self._ledger(fake_supabase)
        self._line(fake_supabase)
        result = await run_admin_reconciliation()
        assert not [f for f in result["findings"] if f["finding_type"] == "provider_cost_delta"]

    @pytest.mark.anyio
    async def test_a_cost_delta_becomes_a_finding(self, fake_supabase):
        self._ledger(fake_supabase, provider_cost_usd=5.0)
        self._line(fake_supabase, raw_cost_usd=1.0)
        result = await run_admin_reconciliation()
        (delta,) = [f for f in result["findings"] if f["finding_type"] == "provider_cost_delta"]
        assert delta["severity"] == "critical", "a delta over a dollar"
        assert delta["delta_cost_usd"] == 4.0

    @pytest.mark.anyio
    async def test_a_small_delta_is_only_a_warning(self, fake_supabase):
        self._ledger(fake_supabase, provider_cost_usd=1.5)
        self._line(fake_supabase, raw_cost_usd=1.0)
        result = await run_admin_reconciliation()
        (delta,) = [f for f in result["findings"] if f["finding_type"] == "provider_cost_delta"]
        assert delta["severity"] == "warning"

    @pytest.mark.anyio
    async def test_a_delta_under_a_cent_is_ignored(self, fake_supabase):
        self._ledger(fake_supabase, provider_cost_usd=1.005)
        self._line(fake_supabase, raw_cost_usd=1.0)
        result = await run_admin_reconciliation()
        assert not [f for f in result["findings"] if f["finding_type"] == "provider_cost_delta"]

    @pytest.mark.anyio
    async def test_an_unattributable_line_becomes_a_finding(self, fake_supabase):
        """A provider charged for a session the ledger has never heard of."""
        self._line(fake_supabase, session_id="room-nobody-knows")
        result = await run_admin_reconciliation()
        assert any(f["finding_type"] == "unmatched_provider_line" for f in result["findings"])

    @pytest.mark.anyio
    async def test_a_line_with_no_session_candidates_is_not_unmatched(self, fake_supabase):
        self._line(fake_supabase, session_id=None, external_reference=None)
        result = await run_admin_reconciliation()
        assert not [f for f in result["findings"] if f["finding_type"] == "unmatched_provider_line"]

    @pytest.mark.anyio
    async def test_an_external_reference_can_match_a_session(self, fake_supabase):
        self._ledger(fake_supabase, session_id="room-1")
        self._line(fake_supabase, session_id=None, external_reference="room-1")
        result = await run_admin_reconciliation()
        assert not [f for f in result["findings"] if f["finding_type"] == "unmatched_provider_line"]

    @pytest.mark.anyio
    async def test_a_session_billed_below_cost_is_critical(self, fake_supabase):
        """The finding that matters: the platform lost money on this session."""
        self._ledger(fake_supabase, billed_cost_usd=0.5, provider_cost_usd=0.5)
        self._line(fake_supabase, raw_cost_usd=5.0)
        result = await run_admin_reconciliation()
        (finding,) = [
            f for f in result["findings"] if f["finding_type"] == "negative_margin_session"
        ]
        assert finding["severity"] == "critical"
        assert finding["session_id"] == "room-1"
        assert finding["delta_cost_usd"] == pytest.approx(-4.5)

    @pytest.mark.anyio
    async def test_a_profitable_session_is_not_flagged(self, fake_supabase):
        self._ledger(fake_supabase, billed_cost_usd=10.0)
        self._line(fake_supabase, raw_cost_usd=1.0)
        result = await run_admin_reconciliation()
        assert not [f for f in result["findings"] if f["finding_type"] == "negative_margin_session"]

    @pytest.mark.anyio
    async def test_provider_filters_narrow_both_sides(self, fake_supabase):
        self._ledger(fake_supabase, model_id="openai/gpt-4o")
        self._ledger(fake_supabase, model_id="anthropic/claude", session_id="room-2")
        self._line(fake_supabase, provider="openai")
        result = await run_admin_reconciliation(provider_filters=["openai"])
        assert {p["provider"] for p in result["summary"]["providers"]} == {"openai"}

    @pytest.mark.anyio
    async def test_an_unsupported_provider_filter_is_refused(self, fake_supabase):
        with pytest.raises(ValueError, match="Unsupported provider"):
            await run_admin_reconciliation(provider_filters=["stripe"])
        assert fake_supabase.db.rows("provider_reconciliation_runs") == []

    @pytest.mark.anyio
    async def test_import_ids_narrow_the_imported_side(self, fake_supabase):
        self._line(fake_supabase, import_id="imp-1", raw_cost_usd=1.0)
        self._line(fake_supabase, import_id="imp-2", raw_cost_usd=99.0)
        result = await run_admin_reconciliation(import_ids=["imp-1"])
        assert result["summary"]["counts"]["imported_usage_lines"] == 1

    @pytest.mark.anyio
    async def test_a_period_narrows_both_sides(self, fake_supabase):
        self._line(fake_supabase, started_at="2026-01-01T00:00:00+00:00")
        self._line(fake_supabase, started_at="2026-03-15T00:00:00+00:00")
        result = await run_admin_reconciliation(period_start=START, period_end=END)
        assert result["summary"]["counts"]["imported_usage_lines"] == 1

    @pytest.mark.anyio
    async def test_the_totals_add_up(self, fake_supabase):
        self._ledger(fake_supabase, provider_cost_usd=1.0, billed_cost_usd=3.0)
        self._line(fake_supabase, raw_cost_usd=1.0)
        totals = (await run_admin_reconciliation())["summary"]["totals"]
        assert totals["imported_cost_usd"] == 1.0
        assert totals["internal_provider_cost_usd"] == 1.0
        assert totals["internal_billed_cost_usd"] == 3.0
        assert totals["realized_margin_usd"] == 2.0

    @pytest.mark.anyio
    async def test_a_failure_marks_the_run_failed_and_re_raises(self, monkeypatch, fake_supabase):
        async def boom(*a, **k):
            raise RuntimeError("fetch exploded")

        monkeypatch.setattr(ar, "_fetch_rows", boom)
        with pytest.raises(RuntimeError, match="fetch exploded"):
            await run_admin_reconciliation()
        (row,) = fake_supabase.db.rows("provider_reconciliation_runs")
        assert row["status"] == "failed"
        assert "fetch exploded" in row["error"]

    @pytest.mark.anyio
    async def test_the_findings_are_persisted(self, fake_supabase):
        self._ledger(fake_supabase, provider_cost_usd=5.0)
        self._line(fake_supabase, raw_cost_usd=1.0)
        result = await run_admin_reconciliation()
        stored = fake_supabase.db.rows("provider_reconciliation_findings")
        assert len(stored) == len(result["findings"])


def test_provider_usage_line_round_trips_through_asdict():
    line = ProviderUsageLine(provider="openai", service="api", usage_unit="usd")
    assert asdict(line)["provider"] == "openai"
    assert _provider_cost(asdict(line)) == 0.0


def test_a_period_helper_tolerates_a_timedelta_derived_window():
    window_end = START + timedelta(days=30)
    assert _filter_period_rows(
        [{"started_at": "2026-03-15T00:00:00Z"}],
        started_at_key="started_at",
        period_start=START,
        period_end=window_end,
    )
