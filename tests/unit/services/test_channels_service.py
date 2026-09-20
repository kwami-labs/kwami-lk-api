"""`src.services.channels` — the gaps the route and webhook tests leave.

Mostly the "insert came back empty" arms and the update-vs-insert forks in the
`ensure_*` / `upsert_*` helpers, which decide whether a second inbound message
creates a duplicate contact or reuses the first.
"""

from __future__ import annotations

import pytest

from src.services import channels as ch
from src.services import idempotency
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


async def _channel(tenant, **overrides):
    """`upsert_channel` with its two other required keyword-only arguments filled."""
    return await upsert_channel(
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

        async def execute(self):
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
    @pytest.mark.anyio
    async def test_an_empty_list_touches_nothing(self, monkeypatch, fake_supabase):
        def explode():
            raise AssertionError("must not reach the database")

        monkeypatch.setattr(ch, "get_supabase_admin", explode)
        assert await delete_kwami_channels("u1", []) is None

    @pytest.mark.anyio
    async def test_it_deletes_the_named_rows(self, fake_supabase, tenant):
        channel = await _channel(tenant, kind="sms")
        await delete_kwami_channels(tenant.user_id, [str(channel["id"])])
        assert fake_supabase.db.rows("kwami_channels") == []

    @pytest.mark.anyio
    async def test_another_users_rows_are_untouched(self, fake_supabase, tenant, other_tenant):
        channel = await _channel(tenant, kind="sms")
        await delete_kwami_channels(other_tenant.user_id, [str(channel["id"])])
        assert len(fake_supabase.db.rows("kwami_channels")) == 1


class TestUpsertChannel:
    @pytest.mark.anyio
    async def test_a_second_upsert_updates_rather_than_duplicates(self, fake_supabase, tenant):
        first = await _channel(tenant, kind="sms", status="pending")
        second = await _channel(tenant, kind="sms", status="active")
        assert second["id"] == first["id"]
        assert second["status"] == "active"
        assert len(fake_supabase.db.rows("kwami_channels")) == 1

    @pytest.mark.anyio
    async def test_an_update_returning_nothing_falls_back_to_the_merged_row(
        self, monkeypatch, fake_supabase, tenant
    ):
        await _channel(tenant, kind="sms")
        real = ch._single
        calls = {"n": 0}

        def once(result):
            calls["n"] += 1
            return real(result) if calls["n"] == 1 else None

        monkeypatch.setattr(ch, "_single", once)
        row = await _channel(tenant, kind="sms", status="active")
        assert row["status"] == "active"

    @pytest.mark.anyio
    async def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create channel"):
            await upsert_channel(
                user_id="u",
                kwami_id="k",
                kind="sms",
                phone_number="+1",
                country_code="US",
                status="active",
            )


class TestUpdateChannel:
    @pytest.mark.anyio
    async def test_it_updates(self, fake_supabase, tenant):
        channel = await _channel(tenant, kind="sms")
        updated = await update_channel(
            str(channel["id"]), user_id=tenant.user_id, updates={"status": "disabled"}
        )
        assert updated["status"] == "disabled"

    @pytest.mark.anyio
    async def test_an_unknown_channel_is_not_found(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Channel not found"):
            await update_channel(
                "00000000-0000-0000-0000-000000000000",
                user_id=tenant.user_id,
                updates={"status": "x"},
            )

    @pytest.mark.anyio
    async def test_another_users_channel_is_not_found(self, fake_supabase, tenant, other_tenant):
        channel = await _channel(tenant, kind="sms")
        with pytest.raises(ValueError, match="Channel not found"):
            await update_channel(
                str(channel["id"]), user_id=other_tenant.user_id, updates={"status": "x"}
            )


class TestChannelLookups:
    async def _channel(self, tenant, **overrides):
        return await _channel(tenant, kind="sms", **overrides)

    @pytest.mark.anyio
    async def test_get_channel(self, fake_supabase, tenant):
        channel = await self._channel(tenant)
        assert (await get_channel(tenant.user_id, str(channel["id"])))["id"] == channel["id"]

    @pytest.mark.anyio
    async def test_get_channel_by_kind(self, fake_supabase, tenant):
        await self._channel(tenant)
        assert await get_channel_by_kind(tenant.user_id, tenant.kwami_id, "sms") is not None

    @pytest.mark.anyio
    async def test_get_channel_by_an_absent_kind_is_none(self, fake_supabase, tenant):
        await self._channel(tenant)
        assert await get_channel_by_kind(tenant.user_id, tenant.kwami_id, "voice_phone") is None

    @pytest.mark.anyio
    async def test_find_by_kind_and_address_matches_the_provider_sender(
        self, fake_supabase, tenant
    ):
        await self._channel(tenant, provider_sender="whatsapp:+14155552671")
        assert await find_channel_by_kind_and_address("sms", "whatsapp:+14155552671") is not None

    @pytest.mark.anyio
    async def test_find_by_kind_and_address_falls_back_to_the_phone_number(
        self, fake_supabase, tenant
    ):
        await self._channel(tenant)
        assert await find_channel_by_kind_and_address("sms", "+14155552671") is not None

    @pytest.mark.anyio
    async def test_find_by_kind_and_address_with_no_match(self, fake_supabase, tenant):
        await self._channel(tenant)
        assert await find_channel_by_kind_and_address("sms", "+19999999999") is None

    @pytest.mark.anyio
    async def test_channels_sharing_a_twilio_incoming_sid(self, fake_supabase, tenant):
        for kind in ("voice_phone", "sms", "whatsapp"):
            await _channel(tenant, kind=kind, provider_channel_sid="PN1")
        rows = await list_channels_sharing_twilio_incoming(tenant.user_id, tenant.kwami_id, "PN1")
        assert len(rows) == 3

    @pytest.mark.anyio
    async def test_an_unknown_sid_shares_nothing(self, fake_supabase, tenant):
        assert (
            await list_channels_sharing_twilio_incoming(
                tenant.user_id, tenant.kwami_id, "PN_unknown"
            )
            == []
        )


class TestEnsureContact:
    @pytest.mark.anyio
    async def test_a_second_call_updates_rather_than_duplicates(self, fake_supabase, tenant):
        first = await ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
            metadata={"a": 1},
        )
        second = await ensure_contact(
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

    @pytest.mark.anyio
    async def test_an_existing_display_name_is_kept_when_none_is_supplied(
        self, fake_supabase, tenant
    ):
        await ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
            display_name="Ada",
        )
        again = await ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
        )
        assert again["display_name"] == "Ada"

    @pytest.mark.anyio
    async def test_an_update_returning_nothing_falls_back_to_the_merged_row(
        self, monkeypatch, fake_supabase, tenant
    ):
        await ensure_contact(
            user_id=tenant.user_id, kwami_id=tenant.kwami_id, phone_number="+14155552671"
        )
        real = ch._single
        calls = {"n": 0}

        def once(result):
            calls["n"] += 1
            return real(result) if calls["n"] == 1 else None

        monkeypatch.setattr(ch, "_single", once)
        row = await ensure_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            phone_number="+14155552671",
            display_name="Ada",
        )
        assert row["display_name"] == "Ada"

    @pytest.mark.anyio
    async def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create contact"):
            await ensure_contact(user_id="u", kwami_id="k", phone_number="+1")


class TestCreateAndUpdateContact:
    @pytest.mark.anyio
    async def test_creating_a_contact(self, fake_supabase, tenant):
        row = await create_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            display_name="Ada",
            phone_number="+14155552671",
        )
        assert row["display_name"] == "Ada"

    @pytest.mark.anyio
    async def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create contact"):
            await create_contact(user_id="u", kwami_id="k", display_name="Ada", phone_number="+1")

    @pytest.mark.anyio
    async def test_updating_a_contact(self, fake_supabase, tenant):
        row = await create_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            display_name="Ada",
            phone_number="+14155552671",
        )
        updated = await update_contact(
            user_id=tenant.user_id,
            contact_id=str(row["id"]),
            updates={"display_name": "Ada L"},
        )
        assert updated["display_name"] == "Ada L"

    @pytest.mark.anyio
    async def test_updating_an_unknown_contact_is_not_found(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Contact not found"):
            await update_contact(
                user_id=tenant.user_id,
                contact_id="00000000-0000-0000-0000-000000000000",
                updates={"display_name": "x"},
            )

    @pytest.mark.anyio
    async def test_updating_another_users_contact_is_not_found(
        self, fake_supabase, tenant, other_tenant
    ):
        row = await create_contact(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            display_name="Ada",
            phone_number="+14155552671",
        )
        with pytest.raises(ValueError, match="Contact not found"):
            await update_contact(
                user_id=other_tenant.user_id,
                contact_id=str(row["id"]),
                updates={"display_name": "x"},
            )


class TestGetContact:
    @pytest.mark.anyio
    async def test_an_unknown_contact_is_not_found(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="Contact not found"):
            await get_contact(tenant.user_id, "00000000-0000-0000-0000-000000000000")


class TestEnsureConversation:
    @pytest.mark.anyio
    async def test_a_second_call_updates_rather_than_duplicates(self, fake_supabase, tenant):
        contact = await ensure_contact(
            user_id=tenant.user_id, kwami_id=tenant.kwami_id, phone_number="+14155552671"
        )
        channel = await _channel(tenant, kind="sms")
        kwargs = dict(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
            contact_id=contact["id"],
        )
        first = await ensure_conversation(**kwargs, metadata={"a": 1})
        second = await ensure_conversation(**kwargs, metadata={"b": 2}, external_thread_id="CA1")
        assert second["id"] == first["id"]
        assert second["metadata"] == {"a": 1, "b": 2}
        assert second["external_thread_id"] == "CA1"

    @pytest.mark.anyio
    async def test_an_existing_thread_id_is_kept(self, fake_supabase, tenant):
        channel = await _channel(tenant, kind="sms")
        kwargs = dict(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )
        await ensure_conversation(**kwargs, external_thread_id="CA1")
        again = await ensure_conversation(**kwargs)
        assert again["external_thread_id"] == "CA1"

    @pytest.mark.anyio
    async def test_an_update_returning_nothing_falls_back_to_the_merged_row(
        self, monkeypatch, fake_supabase, tenant
    ):
        channel = await _channel(tenant, kind="sms")
        kwargs = dict(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )
        await ensure_conversation(**kwargs)
        real = ch._single
        calls = {"n": 0}

        def once(result):
            calls["n"] += 1
            return real(result) if calls["n"] == 1 else None

        monkeypatch.setattr(ch, "_single", once)
        row = await ensure_conversation(**kwargs, status="closed")
        assert row["status"] == "closed"

    @pytest.mark.anyio
    async def test_an_insert_returning_nothing_is_a_runtime_error(self, monkeypatch, fake_supabase):
        monkeypatch.setattr(ch, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create conversation"):
            await ensure_conversation(user_id="u", kwami_id="k", channel_id="c", kind="call")


class TestEvents:
    async def _wiring(self, tenant):
        channel = await _channel(tenant, kind="sms")
        conversation = await ensure_conversation(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )
        return channel, conversation

    @pytest.mark.anyio
    async def test_a_call_event_touches_its_conversation(self, fake_supabase, tenant):
        channel, conversation = await self._wiring(tenant)
        await create_call_event(
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

    @pytest.mark.anyio
    async def test_a_call_event_without_a_conversation_is_still_recorded(
        self, fake_supabase, tenant
    ):
        channel, _ = await self._wiring(tenant)
        row = await create_call_event(
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

    @pytest.mark.anyio
    async def test_a_call_event_insert_returning_nothing_is_a_runtime_error(
        self, monkeypatch, fake_supabase
    ):
        # The insert moved into `insert_or_existing`, so that is the module
        # whose client has to be swapped.
        monkeypatch.setattr(idempotency, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create call event"):
            await create_call_event(
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

    @pytest.mark.anyio
    async def test_a_message_event_insert_returning_nothing_is_a_runtime_error(
        self, monkeypatch, fake_supabase
    ):
        # The insert moved into `insert_or_existing`, so that is the module
        # whose client has to be swapped.
        monkeypatch.setattr(idempotency, "get_supabase_admin", _empty_insert_client)
        with pytest.raises(RuntimeError, match="Failed to create message event"):
            await create_message_event(
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

    @pytest.mark.anyio
    async def test_a_message_event_without_a_conversation_is_still_recorded(
        self, fake_supabase, tenant
    ):
        channel, _ = await self._wiring(tenant)
        row = await create_message_event(
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

    @pytest.mark.anyio
    async def test_updating_a_call_event_with_every_optional_field(self, fake_supabase, tenant):
        channel, conversation = await self._wiring(tenant)
        await create_call_event(
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
        await update_call_event_status(
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

    @pytest.mark.anyio
    async def test_updating_a_call_event_with_no_optional_fields(self, fake_supabase, tenant):
        channel, conversation = await self._wiring(tenant)
        await create_call_event(
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
        await update_call_event_status("CA1", status="completed")
        assert fake_supabase.db.rows("kwami_call_events")[0]["status"] == "completed"

    @pytest.mark.anyio
    async def test_updating_a_message_event_with_every_optional_field(self, fake_supabase, tenant):
        channel, conversation = await self._wiring(tenant)
        await create_message_event(
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
        await update_message_event_status(
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

    @pytest.mark.anyio
    async def test_updating_a_message_event_with_no_optional_fields(self, fake_supabase, tenant):
        channel, conversation = await self._wiring(tenant)
        await create_message_event(
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
        await update_message_event_status("SM1", provider_status="delivered")
        assert fake_supabase.db.rows("kwami_message_events")[0]["provider_status"] == "delivered"


class TestTouchConversation:
    """The None guard lives at the call sites, not in here."""

    async def _conversation(self, tenant):
        channel = await _channel(tenant)
        return await ensure_conversation(
            user_id=tenant.user_id,
            kwami_id=tenant.kwami_id,
            channel_id=channel["id"],
            kind="call",
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("direction", "field"),
        [("inbound", "last_inbound_at"), ("outbound", "last_outbound_at")],
    )
    async def test_it_stamps_the_field_for_the_direction(
        self, fake_supabase, tenant, direction, field
    ):
        conversation = await self._conversation(tenant)
        await touch_conversation(str(conversation["id"]), direction=direction)
        row = fake_supabase.db.rows("kwami_conversations")[0]
        assert row[field] is not None

    @pytest.mark.anyio
    async def test_an_unknown_direction_is_treated_as_outbound(self, fake_supabase, tenant):
        conversation = await self._conversation(tenant)
        await touch_conversation(str(conversation["id"]), direction="sideways")
        assert fake_supabase.db.rows("kwami_conversations")[0]["last_outbound_at"] is not None


class TestRecentEventsForKwami:
    @pytest.mark.anyio
    async def test_an_empty_kwami(self, fake_supabase, tenant):
        events = await recent_events_for_kwami(tenant.user_id, tenant.kwami_id)
        assert events == {"calls": [], "messages": []}

    @pytest.mark.anyio
    async def test_it_returns_both_kinds(self, fake_supabase, tenant):
        channel = await _channel(tenant, kind="sms")
        await create_call_event(
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
        await create_message_event(
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
        events = await recent_events_for_kwami(tenant.user_id, tenant.kwami_id)
        assert len(events["calls"]) == 1
        assert len(events["messages"]) == 1
