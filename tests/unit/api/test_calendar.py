"""`/calendar` and `src.services.calendar_service`.

The service raises `ValueError` for every validation failure and the routes map
that to 400 (or 404 for the one message that means not-found). Ownership goes
through `channels.get_owned_kwami`, which raises a bare `ValueError` too — so a
cross-tenant kwami is reported here as a 400 "Kwami not found" rather than a
404, which is the same legacy seam `/contacts` hits.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.services import calendar_service
from src.services.calendar_service import (
    VALID_EVENT_TYPES,
    _normalize_color,
    _normalize_event_type,
    _parse_iso,
    _single,
    create_event,
    delete_event,
    list_events,
    update_event,
)

pytestmark = pytest.mark.anyio

START = "2026-03-01T10:00:00Z"
END = "2026-03-01T11:00:00Z"


def _payload(kwami_id: str, **overrides) -> dict:
    return {
        "kwami_id": kwami_id,
        "title": "Standup",
        "starts_at": START,
        "ends_at": END,
        **overrides,
    }


# -- helpers -----------------------------------------------------------------


class TestSingle:
    def test_a_list_yields_its_first_row(self):
        assert _single(type("R", (), {"data": [{"id": 1}, {"id": 2}]})()) == {"id": 1}

    def test_an_empty_list_is_none(self):
        assert _single(type("R", (), {"data": []})()) is None

    def test_a_bare_dict_passes_through(self):
        assert _single(type("R", (), {"data": {"id": 1}})()) == {"id": 1}

    def test_a_result_without_data_is_none(self):
        assert _single(object()) is None


class TestParseIso:
    def test_a_zulu_timestamp_is_utc(self):
        assert _parse_iso("2026-03-01T10:00:00Z", "f") == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)

    def test_an_offset_is_preserved(self):
        assert _parse_iso("2026-03-01T10:00:00+02:00", "f").utcoffset().total_seconds() == 7200

    def test_a_naive_timestamp_is_assumed_utc(self):
        """Otherwise a comparison against an aware value raises TypeError."""
        assert _parse_iso("2026-03-01T10:00:00", "f").tzinfo is UTC

    def test_garbage_names_the_field_it_failed_on(self):
        with pytest.raises(ValueError, match="Invalid range_start"):
            _parse_iso("not-a-date", "range_start")


class TestNormalizeEventType:
    @pytest.mark.parametrize("value", sorted(VALID_EVENT_TYPES))
    def test_every_valid_type_is_accepted(self, value):
        assert _normalize_event_type(value) == value

    def test_it_is_case_and_space_insensitive(self):
        assert _normalize_event_type("  MEETING  ") == "meeting"

    def test_none_becomes_other(self):
        assert _normalize_event_type(None) == "other"

    def test_an_unknown_type_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid event_type 'nonsense'"):
            _normalize_event_type("nonsense")


class TestNormalizeColor:
    def test_it_strips(self):
        assert _normalize_color("  #fff  ") == "#fff"

    def test_none_becomes_the_default(self):
        assert _normalize_color(None) == "#6366f1"

    def test_an_overlong_value_is_rejected(self):
        with pytest.raises(ValueError, match="too long"):
            _normalize_color("#" * 33)

    def test_the_length_bound_is_inclusive(self):
        assert _normalize_color("#" * 32) == "#" * 32


# -- service -----------------------------------------------------------------


class TestListEvents:
    async def test_it_returns_events_inside_the_range(self, fake_supabase, tenant):
        await create_event(tenant.user_id, tenant.kwami_id, title="A", starts_at=START, ends_at=END)
        events = await list_events(
            tenant.user_id, tenant.kwami_id, "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z"
        )
        assert [e["title"] for e in events] == ["A"]

    async def test_events_outside_the_range_are_excluded(self, fake_supabase, tenant):
        await create_event(tenant.user_id, tenant.kwami_id, title="A", starts_at=START, ends_at=END)
        assert (
            await list_events(
                tenant.user_id, tenant.kwami_id, "2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z"
            )
            == []
        )

    async def test_an_inverted_range_is_rejected(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="range_end must be after range_start"):
            await list_events(tenant.user_id, tenant.kwami_id, END, START)

    async def test_an_equal_range_is_allowed(self, fake_supabase, tenant):
        assert await list_events(tenant.user_id, tenant.kwami_id, START, START) == []

    async def test_a_malformed_range_is_rejected(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Invalid range_start"):
            await list_events(tenant.user_id, tenant.kwami_id, "nope", END)

    async def test_another_tenants_kwami_is_refused(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(ValueError, match="Kwami not found"):
            await list_events(other_tenant.user_id, tenant.kwami_id, START, END)


class TestCreateEvent:
    async def test_it_normalises_and_stores(self, fake_supabase, tenant):
        row = await create_event(
            tenant.user_id,
            tenant.kwami_id,
            title="  Standup  ",
            starts_at=START,
            ends_at=END,
            description="  notes  ",
            location="  room 1  ",
            event_type="  MEETING ",
            color="  #abc  ",
            all_day=1,
            metadata={"k": "v"},
        )
        assert row["title"] == "Standup"
        assert row["description"] == "notes"
        assert row["location"] == "room 1"
        assert row["event_type"] == "meeting"
        assert row["color"] == "#abc"
        assert row["all_day"] is True
        assert row["metadata"] == {"k": "v"}

    async def test_the_defaults(self, fake_supabase, tenant):
        row = await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
        )
        assert row["event_type"] == "other"
        assert row["color"] == "#6366f1"
        assert row["all_day"] is False
        assert row["metadata"] == {}
        assert row["description"] == ""
        assert row["location"] == ""

    @pytest.mark.parametrize("title", ["", "   "])
    async def test_a_blank_title_is_rejected(self, fake_supabase, tenant, title):
        with pytest.raises(ValueError, match="Title is required"):
            await create_event(
                tenant.user_id, tenant.kwami_id, title=title, starts_at=START, ends_at=END
            )

    async def test_an_inverted_interval_is_rejected(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="ends_at must be after starts_at"):
            await create_event(
                tenant.user_id, tenant.kwami_id, title="T", starts_at=END, ends_at=START
            )

    async def test_a_zero_length_event_is_allowed(self, fake_supabase, tenant):
        assert await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=START
        )

    async def test_an_unknown_event_type_is_rejected(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Invalid event_type"):
            await create_event(
                tenant.user_id,
                tenant.kwami_id,
                title="T",
                starts_at=START,
                ends_at=END,
                event_type="nonsense",
            )

    async def test_another_tenants_kwami_is_refused(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(ValueError, match="Kwami not found"):
            await create_event(
                other_tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
            )

    async def test_an_insert_that_returns_nothing_is_a_runtime_error(
        self, monkeypatch, fake_supabase, tenant
    ):
        monkeypatch.setattr(calendar_service, "_single", lambda r: None)
        with pytest.raises(RuntimeError, match="Failed to create calendar event"):
            await create_event(
                tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
            )


class TestUpdateEvent:
    async def _event(self, tenant):
        return await create_event(
            tenant.user_id,
            tenant.kwami_id,
            title="Original",
            starts_at=START,
            ends_at=END,
            description="d",
            location="l",
        )

    async def test_every_field_can_be_changed(self, fake_supabase, tenant):
        event = await self._event(tenant)
        row = await update_event(
            tenant.user_id,
            str(event["id"]),
            title="  New  ",
            description="  nd  ",
            location="  nl  ",
            all_day=True,
            event_type="task",
            color="#000",
            metadata={"a": 1},
            starts_at="2026-03-02T10:00:00Z",
            ends_at="2026-03-02T12:00:00Z",
        )
        assert row["title"] == "New"
        assert row["description"] == "nd"
        assert row["location"] == "nl"
        assert row["all_day"] is True
        assert row["event_type"] == "task"
        assert row["color"] == "#000"
        assert row["metadata"] == {"a": 1}

    async def test_omitted_fields_are_left_alone(self, fake_supabase, tenant):
        event = await self._event(tenant)
        row = await update_event(tenant.user_id, str(event["id"]), title="Only the title")
        assert row["title"] == "Only the title"
        assert row["description"] == "d"
        assert row["location"] == "l"

    async def test_the_times_are_carried_forward_when_omitted(self, fake_supabase, tenant):
        event = await self._event(tenant)
        row = await update_event(tenant.user_id, str(event["id"]), title="T")
        assert row["starts_at"] == event["starts_at"]
        assert row["ends_at"] == event["ends_at"]

    async def test_moving_only_the_start_is_validated_against_the_stored_end(
        self, fake_supabase, tenant
    ):
        event = await self._event(tenant)
        with pytest.raises(ValueError, match="ends_at must be after starts_at"):
            await update_event(tenant.user_id, str(event["id"]), starts_at="2026-03-01T23:00:00Z")

    async def test_moving_only_the_end_is_validated_against_the_stored_start(
        self, fake_supabase, tenant
    ):
        event = await self._event(tenant)
        with pytest.raises(ValueError, match="ends_at must be after starts_at"):
            await update_event(tenant.user_id, str(event["id"]), ends_at="2026-03-01T09:00:00Z")

    @pytest.mark.parametrize("title", ["", "   "])
    async def test_a_blank_title_is_rejected(self, fake_supabase, tenant, title):
        event = await self._event(tenant)
        with pytest.raises(ValueError, match="Title is required"):
            await update_event(tenant.user_id, str(event["id"]), title=title)

    async def test_an_unknown_event_type_is_rejected(self, fake_supabase, tenant):
        event = await self._event(tenant)
        with pytest.raises(ValueError, match="Invalid event_type"):
            await update_event(tenant.user_id, str(event["id"]), event_type="nonsense")

    async def test_an_overlong_color_is_rejected(self, fake_supabase, tenant):
        event = await self._event(tenant)
        with pytest.raises(ValueError, match="too long"):
            await update_event(tenant.user_id, str(event["id"]), color="#" * 33)

    async def test_an_unknown_event_is_not_found(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Event not found"):
            await update_event(tenant.user_id, "00000000-0000-0000-0000-000000000000", title="T")

    async def test_another_tenants_event_is_not_found(self, fake_supabase, tenant, other_tenant):
        event = await self._event(tenant)
        with pytest.raises(ValueError, match="Event not found"):
            await update_event(other_tenant.user_id, str(event["id"]), title="T")

    async def test_an_update_that_writes_nothing_is_not_found(
        self, monkeypatch, fake_supabase, tenant
    ):
        event = await self._event(tenant)
        calls = {"n": 0}
        real = calendar_service._single

        def once(result):
            calls["n"] += 1
            return real(result) if calls["n"] == 1 else None

        monkeypatch.setattr(calendar_service, "_single", once)
        with pytest.raises(ValueError, match="Event not found"):
            await update_event(tenant.user_id, str(event["id"]), title="T")


class TestDeleteEvent:
    async def test_it_deletes(self, fake_supabase, tenant):
        event = await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
        )
        assert await delete_event(tenant.user_id, str(event["id"])) is True
        assert fake_supabase.db.rows("kwami_calendar_events") == []

    async def test_deleting_nothing_is_false(self, fake_supabase, tenant):
        assert await delete_event(tenant.user_id, "00000000-0000-0000-0000-000000000000") is False

    async def test_another_tenants_event_is_not_deleted(self, fake_supabase, tenant, other_tenant):
        event = await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
        )
        assert await delete_event(other_tenant.user_id, str(event["id"])) is False
        assert len(fake_supabase.db.rows("kwami_calendar_events")) == 1


# -- routes ------------------------------------------------------------------


class TestGetEventsRoute:
    async def test_it_returns_events(self, tenant_client, tenant, fake_supabase):
        await create_event(tenant.user_id, tenant.kwami_id, title="A", starts_at=START, ends_at=END)
        r = await tenant_client.get(
            "/calendar/events",
            params={
                "kwami_id": tenant.kwami_id,
                "range_start": "2026-03-01T00:00:00Z",
                "range_end": "2026-03-02T00:00:00Z",
            },
        )
        assert r.status_code == 200
        assert [e["title"] for e in r.json()["events"]] == ["A"]

    async def test_a_bad_range_is_a_400(self, tenant_client, tenant):
        r = await tenant_client.get(
            "/calendar/events",
            params={"kwami_id": tenant.kwami_id, "range_start": END, "range_end": START},
        )
        assert r.status_code == 400
        assert "range_end must be after" in r.json()["detail"]

    async def test_missing_query_params_are_a_422(self, tenant_client, tenant):
        r = await tenant_client.get("/calendar/events", params={"kwami_id": tenant.kwami_id})
        assert r.status_code == 422

    async def test_it_requires_auth(self, client, tenant):
        r = await client.get(
            "/calendar/events",
            params={"kwami_id": tenant.kwami_id, "range_start": START, "range_end": END},
        )
        assert r.status_code == 401


class TestCreateEventRoute:
    async def test_it_creates(self, tenant_client, tenant):
        r = await tenant_client.post("/calendar/events", json=_payload(tenant.kwami_id))
        assert r.status_code == 200
        assert r.json()["event"]["title"] == "Standup"

    async def test_an_invalid_interval_is_a_400(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/calendar/events", json=_payload(tenant.kwami_id, starts_at=END, ends_at=START)
        )
        assert r.status_code == 400

    async def test_an_invalid_event_type_is_a_400(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/calendar/events", json=_payload(tenant.kwami_id, event_type="nonsense")
        )
        assert r.status_code == 400

    async def test_an_empty_title_is_a_422(self, tenant_client, tenant):
        r = await tenant_client.post("/calendar/events", json=_payload(tenant.kwami_id, title=""))
        assert r.status_code == 422

    async def test_it_requires_auth(self, client, tenant):
        assert (
            await client.post("/calendar/events", json=_payload(tenant.kwami_id))
        ).status_code == 401


class TestPatchEventRoute:
    async def test_it_updates(self, tenant_client, tenant, fake_supabase):
        event = await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
        )
        r = await tenant_client.patch(f"/calendar/events/{event['id']}", json={"title": "Renamed"})
        assert r.status_code == 200
        assert r.json()["event"]["title"] == "Renamed"

    async def test_an_unknown_event_is_a_404(self, tenant_client, tenant):
        r = await tenant_client.patch(
            "/calendar/events/00000000-0000-0000-0000-000000000000", json={"title": "T"}
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "Event not found"

    async def test_a_validation_failure_is_a_400(self, tenant_client, tenant, fake_supabase):
        """The route distinguishes the not-found message from every other ValueError."""
        event = await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
        )
        r = await tenant_client.patch(
            f"/calendar/events/{event['id']}", json={"event_type": "nonsense"}
        )
        assert r.status_code == 400
        assert "Invalid event_type" in r.json()["detail"]

    async def test_it_requires_auth(self, client):
        assert (await client.patch("/calendar/events/x", json={})).status_code == 401


class TestDeleteEventRoute:
    async def test_it_deletes(self, tenant_client, tenant, fake_supabase):
        event = await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
        )
        r = await tenant_client.delete(f"/calendar/events/{event['id']}")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    async def test_an_unknown_event_is_a_404(self, tenant_client):
        r = await tenant_client.delete("/calendar/events/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404

    async def test_another_tenants_event_is_a_404(
        self, tenant_client, other_tenant_client, tenant, fake_supabase
    ):
        event = await create_event(
            tenant.user_id, tenant.kwami_id, title="T", starts_at=START, ends_at=END
        )
        assert (
            await other_tenant_client.delete(f"/calendar/events/{event['id']}")
        ).status_code == 404

    async def test_it_requires_auth(self, client):
        assert (await client.delete("/calendar/events/x")).status_code == 401
