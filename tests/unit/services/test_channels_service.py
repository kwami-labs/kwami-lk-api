"""`src.services.channels` — the gaps the route and webhook tests leave.

Mostly the "insert came back empty" arms and the update-vs-insert forks in the
`ensure_*` / `upsert_*` helpers, which decide whether a second inbound message
creates a duplicate contact or reuses the first.
"""

from __future__ import annotations

import pytest

from src.services import channels as ch
from src.services.channels import (
    _single,
    create_call_event,
    create_contact,
    create_message_event,
    delete_kwami_channels,
    ensure_contact,
    ensure_conversation,
    find_channel_by_kind_and_address,
    get_channel,
    get_channel_by_kind,
    get_contact,
    list_channels_sharing_twilio_incoming,
    recent_events_for_kwami,
    touch_conversation,
    try_normalize_phone_number,
    update_call_event_status,
    update_channel,
    update_contact,
    update_message_event_status,
    upsert_channel,
)


def _channel(tenant, **overrides):
    """`upsert_channel` with its two other required keyword-only arguments filled."""
    return upsert_channel(
        user_id=overrides.pop("user_id", tenant.user_id),
        kwami_id=overrides.pop("kwami_id", tenant.kwami_id),
        kind=overrides.pop("kind", "sms"),
        phone_number=overrides.pop("phone_number", "+14155552671"),
        country_code=overrides.pop("country_code", "US"),
        status=overrides.pop("status", "active"),
        **overrides,
    )


def _empty_insert_client():
    class _T:
        def insert(self, payload):
            return self

        def update(self, payload):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def order(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    return type("C", (), {"table": staticmethod(lambda n: _T())})()


class TestSingle:
    def test_a_list_yields_its_first_row(self):
        assert _single(type("R", (), {"data": [{"id": 1}]})()) == {"id": 1}

    def test_an_empty_list_is_none(self):
        assert _single(type("R", (), {"data": []})()) is None

    def test_a_bare_dict_passes_through(self):
        assert _single(type("R", (), {"data": {"id": 1}})()) == {"id": 1}

    def test_no_data_attribute_is_none(self):
        assert _single(object()) is None


class TestTryNormalizePhoneNumber:
    def test_a_valid_number(self):
        assert try_normalize_phone_number("(415) 555-2671", "US") == "+14155552671"

    def test_an_unparseable_number_is_none_rather_than_a_raise(self):
        """Inbound webhooks get whatever the network sent; a raise means retries."""
        assert try_normalize_phone_number("anonymous", "US") is None

    def test_a_parseable_but_invalid_number_is_none(self):
        assert try_normalize_phone_number("+1555", "US") is None

    @pytest.mark.parametrize("value", ["", None])
    def test_an_empty_value_is_none(self, value):
        assert try_normalize_phone_number(value, "US") is None


class TestDeleteKwamiChannels:
    def test_an_empty_list_touches_nothing(self, monkeypatch, fake_supabase):
        def explode():
            raise AssertionError("must not reach the database")

        monkeypatch.setattr(ch, "get_supabase_admin", explode)
        assert delete_kwami_channels("u1", []) is None

    def test_it_deletes_the_named_rows(self, fake_supabase, tenant):
        channel = _channel(tenant, kind="sms")
        delete_kwami_channels(tenant.user_id, [str(channel["id"])])
        assert fake_supabase.db.rows("kwami_channels") == []

    def test_another_users_rows_are_untouched(self, fake_supabase, tenant, other_tenant):
        channel = _channel(tenant, kind="sms")
        delete_kwami_channels(other_tenant.user_id, [str(channel["id"])])
        assert len(fake_supabase.db.rows("kwami_channels")) == 1


class TestUpsertChannel:
    def test_a_second_upsert_updates_rather_than_duplicates(self, fake_supabase, tenant):
        first = _channel(tenant, kind="sms", status="pending")
        second = _channel(tenant, kind="sms", status="active")
        assert second["id"] == first["id"]
        assert second["status"] == "active"
        assert len(fake_supabase.db.rows("kwami_channels")) == 1

    def test_an_update_returning_nothing_falls_back_to_the_merged_row(
        self, monkeypatch, fake_supabase, tenant
    ):
        _channel(tenant, kind="sms")
        real = ch._single
        calls = {"n": 0}

        def once(result):
            calls["n"] += 1
            return real(result) if calls["n"] == 1 else None

        monkeypatch.setattr(ch, "_single", once)
        row = _channel(tenant, kind="sms", status="active")
        assert row["status"] == "active"

    def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create channel"):
            upsert_channel(
                user_id="u",
                kwami_id="k",
                kind="sms",
                phone_number="+1",
                country_code="US",
                status="active",
            )


class TestUpdateChannel:
    def test_it_updates(self, fake_supabase, tenant):
        channel = _channel(tenant, kind="sms")
        updated = update_channel(
            str(channel["id"]), user_id=tenant.user_id, updates={"status": "disabled"}
        )
        assert updated["status"] == "disabled"

    def test_an_unknown_channel_is_not_found(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Channel not found"):
            update_channel(
                "00000000-0000-0000-0000-000000000000",
                user_id=tenant.user_id,
                updates={"status": "x"},
            )

    def test_another_users_channel_is_not_found(self, fake_supabase, tenant, other_tenant):
        channel = _channel(tenant, kind="sms")
        with pytest.raises(ValueError, match="Channel not found"):
            update_channel(
                str(channel["id"]), user_id=other_tenant.user_id, updates={"status": "x"}
            )


class TestChannelLookups:
    def _channel(self, tenant, **overrides):
        return _channel(tenant, kind="sms", **overrides)

    def test_get_channel(self, fake_supabase, tenant):
        channel = self._channel(tenant)
        assert get_channel(tenant.user_id, str(channel["id"]))["id"] == channel["id"]

    def test_get_channel_by_kind(self, fake_supabase, tenant):
        self._channel(tenant)
        assert get_channel_by_kind(tenant.user_id, tenant.kwami_id, "sms") is not None

    def test_get_channel_by_an_absent_kind_is_none(self, fake_supabase, tenant):
        self._channel(tenant)
        assert get_channel_by_kind(tenant.user_id, tenant.kwami_id, "voice_phone") is None

    def test_find_by_kind_and_address_matches_the_provider_sender(self, fake_supabase, tenant):
        self._channel(tenant, provider_sender="whatsapp:+14155552671")
        assert find_channel_by_kind_and_address("sms", "whatsapp:+14155552671") is not None

    def test_find_by_kind_and_address_falls_back_to_the_phone_number(self, fake_supabase, tenant):
        self._channel(tenant)
        assert find_channel_by_kind_and_address("sms", "+14155552671") is not None

    def test_find_by_kind_and_address_with_no_match(self, fake_supabase, tenant):
        self._channel(tenant)
        assert find_channel_by_kind_and_address("sms", "+19999999999") is None

    def test_channels_sharing_a_twilio_incoming_sid(self, fake_supabase, tenant):
        for kind in ("voice_phone", "sms", "whatsapp"):
            _channel(tenant, kind=kind, provider_channel_sid="PN1")
        rows = list_channels_sharing_twilio_incoming(tenant.user_id, tenant.kwami_id, "PN1")
        assert len(rows) == 3

    def test_an_unknown_sid_shares_nothing(self, fake_supabase, tenant):
        assert (
            list_channels_sharing_twilio_incoming(tenant.user_id, tenant.kwami_id, "PN_unknown")
            == []
        )


class TestEnsureContact:
    def test_a_second_call_updates_rather_than_duplicates(self, fake_supabase, tenant):
        first = ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
            metadata={"a": 1},
        )
        second = ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
            display_name="Ada",
            metadata={"b": 2},
        )
        assert second["id"] == first["id"]
        assert second["display_name"] == "Ada"
        assert second["metadata"] == {"a": 1, "b": 2}, "metadata merges"
        assert len(fake_supabase.db.rows("kwami_contacts")) == 1

    def test_an_existing_display_name_is_kept_when_none_is_supplied(self, fake_supabase, tenant):
        ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
            display_name="Ada",
        )
        again = ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
        )
        assert again["display_name"] == "Ada"

    def test_an_update_returning_nothing_falls_back_to_the_merged_row(
        self, monkeypatch, fake_supabase, tenant
    ):
        ensure_contact(
            user_id=tenant.user_id, kwami_id=tenant.kwami_id, phone_number="+14155552671"
        )
        real = ch._single
        calls = {"n": 0}

        def once(result):
            calls["n"] += 1
            return real(result) if calls["n"] == 1 else None

        monkeypatch.setattr(ch, "_single", once)
        row = ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
            display_name="Ada",
        )
        assert row["display_name"] == "Ada"

    def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create contact"):
            ensure_contact(user_id="u", kwami_id="k", phone_number="+1")


class TestCreateAndUpdateContact:
    def test_creating_a_contact(self, fake_supabase, tenant):
        row = create_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            display_name="Ada",
            phone_number="+14155552671",
        )
        assert row["display_name"] == "Ada"

    def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create contact"):
            create_contact(user_id="u", kwami_id="k", display_name="Ada", phone_number="+1")

    def test_updating_a_contact(self, fake_supabase, tenant):
        row = create_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            display_name="Ada",
            phone_number="+14155552671",
        )
        updated = update_contact(
            user_id=tenant.user_id,
            contact_id=str(row["id"]),
            updates={"display_name": "Ada L"},
        )
        assert updated["display_name"] == "Ada L"

    def test_updating_an_unknown_contact_is_not_found(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Contact not found"):
            update_contact(
                user_id=tenant.user_id,
                contact_id="00000000-0000-0000-0000-000000000000",
                updates={"display_name": "x"},
            )

    def test_updating_another_users_contact_is_not_found(self, fake_supabase, tenant, other_tenant):
        row = create_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            display_name="Ada",
            phone_number="+14155552671",
        )
        with pytest.raises(ValueError, match="Contact not found"):
            update_contact(
                user_id=other_tenant.user_id,
                contact_id=str(row["id"]),
                updates={"display_name": "x"},
            )


class TestGetContact:
    def test_an_unknown_contact_is_not_found(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Contact not found"):
            get_contact(tenant.user_id, "00000000-0000-0000-0000-000000000000")


class TestEnsureConversation:
    def test_a_second_call_updates_rather_than_duplicates(self, fake_supabase, tenant):
        contact = ensure_contact(
            user_id=tenant.user_id, kwami_id=tenant.kwami_id, phone_number="+14155552671"
        )
        channel = _channel(tenant, kind="sms")
        kwargs = dict(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
            contact_id=contact["id"],
        )
        first = ensure_conversation(**kwargs, metadata={"a": 1})
        second = ensure_conversation(**kwargs, metadata={"b": 2}, external_thread_id="CA1")
        assert second["id"] == first["id"]
        assert second["metadata"] == {"a": 1, "b": 2}
        assert second["external_thread_id"] == "CA1"

    def test_an_existing_thread_id_is_kept(self, fake_supabase, tenant):
        channel = _channel(tenant, kind="sms")
        kwargs = dict(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )
        ensure_conversation(**kwargs, external_thread_id="CA1")
        again = ensure_conversation(**kwargs)
        assert again["external_thread_id"] == "CA1"

    def test_an_update_returning_nothing_falls_back_to_the_merged_row(
        self, monkeypatch, fake_supabase, tenant
    ):
        channel = _channel(tenant, kind="sms")
        kwargs = dict(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )
        ensure_conversation(**kwargs)
        real = ch._single
        calls = {"n": 0}

        def once(result):
            calls["n"] += 1
            return real(result) if calls["n"] == 1 else None

        monkeypatch.setattr(ch, "_single", once)
        row = ensure_conversation(**kwargs, status="closed")
        assert row["status"] == "closed"

    def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create conversation"):
            ensure_conversation(user_id="u", kwami_id="k", channel_id="c", kind="call")


class TestEvents:
    def _wiring(self, tenant):
        channel = _channel(tenant, kind="sms")
        conversation = ensure_conversation(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )
        return channel, conversation

    def test_a_call_event_touches_its_conversation(self, fake_supabase, tenant):
        channel, conversation = self._wiring(tenant)
        create_call_event(
            conversation_id=conversation["id"],
            channel_id=channel["id"],
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="outbound",
            provider_call_sid="CA1",
            livekit_room_name=None,
            participant_identity=None,
            from_number="+1",
            to_number="+2",
            status="queued",
        )
        assert len(fake_supabase.db.rows("kwami_call_events")) == 1

    def test_a_call_event_without_a_conversation_is_still_recorded(self, fake_supabase, tenant):
        channel, _ = self._wiring(tenant)
        row = create_call_event(
            conversation_id=None,
            channel_id=channel["id"],
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="outbound",
            provider_call_sid="CA1",
            livekit_room_name=None,
            participant_identity=None,
            from_number="+1",
            to_number="+2",
            status="queued",
        )
        assert row["provider_call_sid"] == "CA1"

    def test_a_call_event_insert_returning_nothing_is_a_runtime_error(
        self, monkeypatch, fake_supabase
    ):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create call event"):
            create_call_event(
                conversation_id=None,
                channel_id="c",
                user_id="u",
                kwami_id="k",
                direction="outbound",
                provider_call_sid=None,
                livekit_room_name=None,
                participant_identity=None,
                from_number="+1",
                to_number="+2",
                status="queued",
            )

    def test_a_message_event_insert_returning_nothing_is_a_runtime_error(
        self, monkeypatch, fake_supabase
    ):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create message event"):
            create_message_event(
                conversation_id=None,
                channel_id="c",
                contact_id=None,
                user_id="u",
                kwami_id="k",
                direction="outbound",
                provider_message_sid=None,
                provider_status="queued",
                from_address="+1",
                to_address="+2",
                body="x",
            )

    def test_a_message_event_without_a_conversation_is_still_recorded(self, fake_supabase, tenant):
        channel, _ = self._wiring(tenant)
        row = create_message_event(
            conversation_id=None,
            channel_id=channel["id"],
            contact_id=None,
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="outbound",
            provider_message_sid="SM1",
            provider_status="queued",
            from_address="+1",
            to_address="+2",
            body="x",
        )
        assert row["provider_message_sid"] == "SM1"

    def test_updating_a_call_event_with_every_optional_field(self, fake_supabase, tenant):
        channel, conversation = self._wiring(tenant)
        create_call_event(
            conversation_id=conversation["id"],
            channel_id=channel["id"],
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="inbound",
            provider_call_sid="CA1",
            livekit_room_name=None,
            participant_identity=None,
            from_number="+1",
            to_number="+2",
            status="ringing",
        )
        update_call_event_status(
            "CA1",
            status="failed",
            duration_seconds=7,
            error_code="13224",
            error_message="busy",
            provider_payload={"raw": 1},
        )
        event = fake_supabase.db.rows("kwami_call_events")[0]
        assert event["status"] == "failed"
        assert event["duration_seconds"] == 7
        assert event["provider_payload"] == {"raw": 1}

    def test_updating_a_call_event_with_no_optional_fields(self, fake_supabase, tenant):
        channel, conversation = self._wiring(tenant)
        create_call_event(
            conversation_id=conversation["id"],
            channel_id=channel["id"],
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="inbound",
            provider_call_sid="CA1",
            livekit_room_name=None,
            participant_identity=None,
            from_number="+1",
            to_number="+2",
            status="ringing",
        )
        update_call_event_status("CA1", status="completed")
        assert fake_supabase.db.rows("kwami_call_events")[0]["status"] == "completed"

    def test_updating_a_message_event_with_every_optional_field(self, fake_supabase, tenant):
        channel, conversation = self._wiring(tenant)
        create_message_event(
            conversation_id=conversation["id"],
            channel_id=channel["id"],
            contact_id=None,
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="outbound",
            provider_message_sid="SM1",
            provider_status="queued",
            from_address="+1",
            to_address="+2",
            body="x",
        )
        update_message_event_status(
            "SM1",
            provider_status="failed",
            error_code="30008",
            error_message="unknown destination",
            provider_payload={"raw": 1},
        )
        event = fake_supabase.db.rows("kwami_message_events")[0]
        assert event["provider_status"] == "failed"
        assert event["error_code"] == "30008"
        assert event["provider_payload"] == {"raw": 1}

    def test_updating_a_message_event_with_no_optional_fields(self, fake_supabase, tenant):
        channel, conversation = self._wiring(tenant)
        create_message_event(
            conversation_id=conversation["id"],
            channel_id=channel["id"],
            contact_id=None,
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="outbound",
            provider_message_sid="SM1",
            provider_status="queued",
            from_address="+1",
            to_address="+2",
            body="x",
        )
        update_message_event_status("SM1", provider_status="delivered")
        assert fake_supabase.db.rows("kwami_message_events")[0]["provider_status"] == "delivered"


class TestTouchConversation:
    """The None guard lives at the call sites, not in here."""

    def _conversation(self, tenant):
        channel = _channel(tenant)
        return ensure_conversation(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )

    @pytest.mark.parametrize(
        ("direction", "field"),
        [("inbound", "last_inbound_at"), ("outbound", "last_outbound_at")],
    )
    def test_it_stamps_the_field_for_the_direction(self, fake_supabase, tenant, direction, field):
        conversation = self._conversation(tenant)
        touch_conversation(str(conversation["id"]), direction=direction)
        row = fake_supabase.db.rows("kwami_conversations")[0]
        assert row[field] is not None

    def test_an_unknown_direction_is_treated_as_outbound(self, fake_supabase, tenant):
        conversation = self._conversation(tenant)
        touch_conversation(str(conversation["id"]), direction="sideways")
        assert fake_supabase.db.rows("kwami_conversations")[0]["last_outbound_at"] is not None


class TestRecentEventsForKwami:
    def test_an_empty_kwami(self, fake_supabase, tenant):
        events = recent_events_for_kwami(tenant.user_id, tenant.kwami_id)
        assert events == {"calls": [], "messages": []}

    def test_it_returns_both_kinds(self, fake_supabase, tenant):
        channel = _channel(tenant, kind="sms")
        create_call_event(
            conversation_id=None,
            channel_id=channel["id"],
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="inbound",
            provider_call_sid="CA1",
            livekit_room_name=None,
            participant_identity=None,
            from_number="+1",
            to_number="+2",
            status="ringing",
        )
        create_message_event(
            conversation_id=None,
            channel_id=channel["id"],
            contact_id=None,
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            direction="inbound",
            provider_message_sid="SM1",
            provider_status="received",
            from_address="+1",
            to_address="+2",
            body="x",
        )
        events = recent_events_for_kwami(tenant.user_id, tenant.kwami_id)
        assert len(events["calls"]) == 1
        assert len(events["messages"]) == 1
