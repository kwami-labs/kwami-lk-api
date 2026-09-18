"""`src.services.telephony` — LiveKit SIP trunk sync and outbound calling.

The LiveKit API is replaced with a stand-in: these tests are about which trunk
fields this module writes, and about the two TwirpError paths, which are the
only place a LiveKit failure becomes a client-facing message. The messages are
long and operational on purpose (they tell an operator to go find the PCAP), so
they are asserted on rather than treated as opaque.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from livekit.api.twirp_client import TwirpError

from src.services import telephony
from src.services.telephony import (
    _merged_numbers,
    _numbers_without,
    _response_items,
    _twirp_sip_client_detail,
    build_call_room_name,
    create_outbound_call,
    remove_phone_from_shared_livekit_trunks,
    sync_shared_livekit_trunks,
)

pytestmark = pytest.mark.anyio


def _twirp(code="unavailable", message="no route", status=503, metadata=None):
    """`metadata` is a read-only property, so it goes through the constructor."""
    return TwirpError(code, message, status=status, metadata=metadata or {})


class _Trunk:
    def __init__(self, sip_trunk_id, numbers=None):
        self.sip_trunk_id = sip_trunk_id
        self.numbers = numbers or []


class _Sip:
    def __init__(self, outbound=None, inbound=None):
        self.outbound_trunks = outbound or []
        self.inbound_trunks = inbound or []
        self.outbound_updates: list[tuple] = []
        self.inbound_updates: list[tuple] = []
        self.create_error: Exception | None = None
        self.created_request = None

    async def list_outbound_trunk(self, request):
        return type("R", (), {"items": self.outbound_trunks})()

    async def list_inbound_trunk(self, request):
        return type("R", (), {"items": self.inbound_trunks})()

    async def update_outbound_trunk_fields(self, trunk_id, *, numbers):
        self.outbound_updates.append((trunk_id, numbers))

    async def update_inbound_trunk_fields(self, trunk_id, *, numbers):
        self.inbound_updates.append((trunk_id, numbers))

    async def create_sip_participant(self, request):
        self.created_request = request
        if self.create_error:
            raise self.create_error
        return type(
            "P",
            (),
            {"participant_identity": request.participant_identity, "sip_call_id": "SCL_123"},
        )()


class _Dispatch:
    def __init__(self):
        self.error: Exception | None = None
        self.request = None

    async def create_dispatch(self, request):
        self.request = request
        if self.error:
            raise self.error
        return type("D", (), {"id": "AD_1"})()


class _LkApi:
    def __init__(self, sip=None, dispatch=None):
        self.sip = sip or _Sip()
        self.agent_dispatch = dispatch or _Dispatch()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def lkapi(monkeypatch):
    api = _LkApi()
    monkeypatch.setattr(telephony.livekit_api, "LiveKitAPI", lambda url, key, secret: api)
    return api


@pytest.fixture
def trunks(monkeypatch):
    monkeypatch.setattr(
        telephony.settings, "livekit_sip_outbound_trunk_id", "ST_out", raising=False
    )
    monkeypatch.setattr(telephony.settings, "livekit_sip_inbound_trunk_id", "ST_in", raising=False)


class TestBuildCallRoomName:
    def test_it_is_namespaced_by_kwami(self):
        assert build_call_room_name("abcdef1234").startswith("kwami-call-abcdef12-")

    def test_it_is_unguessable_and_unique(self):
        names = {build_call_room_name("k1") for _ in range(200)}
        assert len(names) == 200
        assert all(re.fullmatch(r"kwami-call-k1-[0-9a-f]{10}", n) for n in names)


class TestResponseItems:
    def test_the_items_field(self):
        assert _response_items(type("R", (), {"items": [1, 2]})()) == [1, 2]

    def test_the_trunks_field(self):
        assert _response_items(type("R", (), {"trunks": [3]})()) == [3]

    def test_items_wins_when_both_are_set(self):
        assert _response_items(type("R", (), {"items": [1], "trunks": [2]})()) == [1]

    def test_an_empty_items_field_falls_through_to_trunks(self):
        assert _response_items(type("R", (), {"items": [], "trunks": [2]})()) == [2]

    def test_neither_field(self):
        assert _response_items(object()) == []


class TestNumberMerging:
    def test_a_new_number_is_appended(self):
        assert _merged_numbers(_Trunk("t", ["+1"]), "+2") == ["+1", "+2"]

    def test_an_existing_number_is_not_duplicated(self):
        assert _merged_numbers(_Trunk("t", ["+1", "+2"]), "+2") == ["+1", "+2"]

    def test_order_is_preserved(self):
        assert _merged_numbers(_Trunk("t", ["+3", "+1"]), "+2") == ["+3", "+1", "+2"]

    def test_a_trunk_with_no_numbers(self):
        assert _merged_numbers(_Trunk("t", None), "+1") == ["+1"]

    def test_removal(self):
        assert _numbers_without(_Trunk("t", ["+1", "+2"]), "+1") == ["+2"]

    def test_removing_something_absent_is_a_no_op(self):
        assert _numbers_without(_Trunk("t", ["+1"]), "+9") == ["+1"]

    def test_removal_drops_every_copy(self):
        assert _numbers_without(_Trunk("t", ["+1", "+1", "+2"]), "+1") == ["+2"]


class TestTwirpSipClientDetail:
    def test_metadata_is_rendered_sorted(self):
        detail = _twirp_sip_client_detail(_twirp(metadata={"b": "2", "a": "1"}))
        assert detail.startswith(" (a=1; b=2)")

    def test_no_metadata_still_carries_the_hint(self):
        detail = _twirp_sip_client_detail(_twirp(metadata={}))
        assert not detail.startswith(" (")
        assert "PCAP" in detail

    def test_the_hint_names_where_to_look(self):
        detail = _twirp_sip_client_detail(_twirp())
        assert "Telephony" in detail and "SCL_" in detail


class TestSyncSharedTrunks:
    async def test_both_trunks_are_updated(self, lkapi, trunks):
        lkapi.sip.outbound_trunks = [_Trunk("ST_out", ["+1"])]
        lkapi.sip.inbound_trunks = [_Trunk("ST_in", [])]

        sync = await sync_shared_livekit_trunks("+2")

        assert sync["outbound"] == {"configured": True, "synced": True, "numberCount": 2}
        assert sync["inbound"] == {"configured": True, "synced": True, "numberCount": 1}
        assert lkapi.sip.outbound_updates == [("ST_out", ["+1", "+2"])]
        assert lkapi.sip.inbound_updates == [("ST_in", ["+2"])]

    async def test_a_missing_outbound_trunk_is_a_503(self, lkapi, trunks):
        lkapi.sip.outbound_trunks = []
        with pytest.raises(HTTPException) as exc:
            await sync_shared_livekit_trunks("+2")
        assert exc.value.status_code == 503
        assert "outbound SIP trunk was not found" in exc.value.detail

    async def test_a_missing_inbound_trunk_is_a_503(self, lkapi, trunks):
        lkapi.sip.outbound_trunks = [_Trunk("ST_out")]
        lkapi.sip.inbound_trunks = []
        with pytest.raises(HTTPException) as exc:
            await sync_shared_livekit_trunks("+2")
        assert "inbound SIP trunk was not found" in exc.value.detail

    async def test_no_outbound_trunk_configured_is_a_note_not_a_failure(self, lkapi, monkeypatch):
        monkeypatch.setattr(
            telephony.settings, "livekit_sip_outbound_trunk_id", None, raising=False
        )
        monkeypatch.setattr(telephony.settings, "livekit_sip_inbound_trunk_id", None, raising=False)
        sync = await sync_shared_livekit_trunks("+2")
        assert sync["outbound"]["synced"] is False
        assert any("outbound trunk is configured" in n for n in sync["notes"])

    async def test_no_inbound_trunk_falls_back_to_the_voice_webhook(self, lkapi, monkeypatch):
        """Inbound routing does not need a per-number allowlist."""
        monkeypatch.setattr(
            telephony.settings, "livekit_sip_outbound_trunk_id", None, raising=False
        )
        monkeypatch.setattr(telephony.settings, "livekit_sip_inbound_trunk_id", None, raising=False)
        sync = await sync_shared_livekit_trunks("+2")
        assert sync["inbound"] == {
            "configured": False,
            "synced": True,
            "strategy": "shared_voice_webhook",
        }
        assert any("shared Twilio voice webhook" in n for n in sync["notes"])

    async def test_the_right_trunk_is_picked_out_of_several(self, lkapi, trunks):
        """The loop has to skip past trunks belonging to other configurations."""
        lkapi.sip.outbound_trunks = [
            _Trunk("ST_other", ["+9"]),
            _Trunk("ST_out", ["+1"]),
            _Trunk("ST_another", ["+8"]),
        ]
        lkapi.sip.inbound_trunks = [_Trunk("ST_wrong", ["+7"]), _Trunk("ST_in", [])]

        await sync_shared_livekit_trunks("+2")

        assert lkapi.sip.outbound_updates == [("ST_out", ["+1", "+2"])]
        assert lkapi.sip.inbound_updates == [("ST_in", ["+2"])]

    async def test_a_list_of_only_non_matching_trunks_is_a_503(self, lkapi, trunks):
        lkapi.sip.outbound_trunks = [_Trunk("ST_other"), _Trunk("ST_another")]
        with pytest.raises(HTTPException):
            await sync_shared_livekit_trunks("+2")

    async def test_a_trunk_list_using_the_trunks_field(self, lkapi, trunks):
        lkapi.sip.list_outbound_trunk = _returning({"trunks": [_Trunk("ST_out", [])]})
        lkapi.sip.inbound_trunks = [_Trunk("ST_in", [])]
        sync = await sync_shared_livekit_trunks("+2")
        assert sync["outbound"]["synced"] is True


class TestRemoveFromSharedTrunks:
    async def test_the_number_is_removed_from_both(self, lkapi, trunks):
        lkapi.sip.outbound_trunks = [_Trunk("ST_out", ["+1", "+2"])]
        lkapi.sip.inbound_trunks = [_Trunk("ST_in", ["+2"])]

        sync = await remove_phone_from_shared_livekit_trunks("+2")

        assert sync["outbound"] == {"configured": True, "synced": True, "numberCount": 1}
        assert sync["inbound"] == {"configured": True, "synced": True, "numberCount": 0}
        assert lkapi.sip.outbound_updates == [("ST_out", ["+1"])]
        assert lkapi.sip.inbound_updates == [("ST_in", [])]

    async def test_a_missing_outbound_trunk_is_warned_about_not_raised(self, lkapi, trunks, caplog):
        """Removal is cleanup -- a missing trunk must not block releasing a number."""
        lkapi.sip.outbound_trunks = []
        lkapi.sip.inbound_trunks = [_Trunk("ST_in", ["+2"])]
        with caplog.at_level("WARNING", logger="kwami-api.telephony"):
            sync = await remove_phone_from_shared_livekit_trunks("+2")
        assert sync["outbound"]["synced"] is False
        assert "outbound trunk ST_out not found" in caplog.text

    async def test_a_missing_inbound_trunk_is_warned_about_not_raised(self, lkapi, trunks, caplog):
        lkapi.sip.outbound_trunks = [_Trunk("ST_out", ["+2"])]
        lkapi.sip.inbound_trunks = []
        with caplog.at_level("WARNING", logger="kwami-api.telephony"):
            sync = await remove_phone_from_shared_livekit_trunks("+2")
        assert sync["inbound"]["synced"] is False
        assert "inbound trunk ST_in not found" in caplog.text

    async def test_nothing_configured_does_nothing(self, lkapi, monkeypatch):
        monkeypatch.setattr(
            telephony.settings, "livekit_sip_outbound_trunk_id", None, raising=False
        )
        monkeypatch.setattr(telephony.settings, "livekit_sip_inbound_trunk_id", None, raising=False)
        sync = await remove_phone_from_shared_livekit_trunks("+2")
        assert sync["outbound"]["synced"] is False
        assert sync["inbound"]["synced"] is False
        assert lkapi.sip.outbound_updates == []


class TestCreateOutboundCall:
    async def test_an_unconfigured_trunk_is_a_503(self, monkeypatch):
        monkeypatch.setattr(
            telephony.settings, "livekit_sip_outbound_trunk_id", None, raising=False
        )
        with pytest.raises(HTTPException) as exc:
            await create_outbound_call(
                kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
            )
        assert exc.value.status_code == 503
        assert "LIVEKIT_SIP_OUTBOUND_TRUNK_ID" in exc.value.detail

    async def test_a_successful_call_returns_its_identifiers(self, lkapi, trunks, monkeypatch):
        monkeypatch.setattr(telephony.settings, "livekit_agent_name", "kwami-agent", raising=False)
        result = await create_outbound_call(
            kwami_id="k1",
            phone_number="+14155552671",
            caller_id="+14155552672",
            participant_name="Ada",
        )
        assert result["room_name"].startswith("kwami-call-k1-")
        assert result["participant_identity"].startswith("sip_")
        assert result["provider_call_sid"] == "SCL_123"
        assert result["agent_dispatch_id"] == "AD_1"

    async def test_the_request_carries_the_kwami_attributes(self, lkapi, trunks, monkeypatch):
        """The agent reads kwami_id off the participant attributes."""
        monkeypatch.setattr(
            telephony.settings,
            "livekit_sip_participant_attribute_key",
            "kwami_id",
            raising=False,
        )
        await create_outbound_call(
            kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
        )
        request = lkapi.sip.created_request
        assert request.sip_trunk_id == "ST_out"
        assert request.sip_call_to == "+1"
        assert request.sip_number == "+2"
        assert request.participant_attributes["kwami_id"] == "k1"
        assert request.participant_attributes["channel"] == "voice_phone"
        assert json.loads(request.participant_metadata)["kwami_id"] == "k1"
        assert request.play_dialtone is True
        assert request.wait_until_answered is False

    async def test_wait_until_answered_is_passed_through(self, lkapi, trunks):
        await create_outbound_call(
            kwami_id="k1",
            phone_number="+1",
            caller_id="+2",
            participant_name="Ada",
            wait_until_answered=True,
        )
        assert lkapi.sip.created_request.wait_until_answered is True

    async def test_the_dispatch_carries_the_kwami_metadata(self, lkapi, trunks, monkeypatch):
        monkeypatch.setattr(
            telephony.settings, "livekit_agent_name", "special-agent", raising=False
        )
        await create_outbound_call(
            kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
        )
        request = lkapi.agent_dispatch.request
        assert request.agent_name == "special-agent"
        assert json.loads(request.metadata) == {"kwami_id": "k1", "channel": "voice_phone"}

    async def test_a_sip_failure_is_a_502_naming_the_twirp_code(self, lkapi, trunks, caplog):
        lkapi.sip.create_error = _twirp(
            code="unavailable", message="no route", metadata={"sip_status": "503"}
        )
        with caplog.at_level("WARNING", logger="kwami-api.telephony"):
            with pytest.raises(HTTPException) as exc:
                await create_outbound_call(
                    kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
                )
        assert exc.value.status_code == 502
        assert "unavailable: no route" in exc.value.detail
        assert "sip_status=503" in exc.value.detail
        assert "PCAP" in exc.value.detail
        assert "CreateSIPParticipant failed" in caplog.text

    async def test_a_sip_failure_without_a_message(self, lkapi, trunks):
        lkapi.sip.create_error = _twirp(code="internal", message="")
        with pytest.raises(HTTPException) as exc:
            await create_outbound_call(
                kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
            )
        assert "unknown error" in exc.value.detail

    async def test_a_dispatch_failure_says_the_leg_was_created(self, lkapi, trunks, caplog):
        """The call is up but nobody is on it -- the message has to say so."""
        lkapi.agent_dispatch.error = _twirp(
            code="not_found", message="no worker", metadata={"agent": "kwami-agent"}
        )
        with caplog.at_level("WARNING", logger="kwami-api.telephony"):
            with pytest.raises(HTTPException) as exc:
                await create_outbound_call(
                    kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
                )
        assert exc.value.status_code == 502
        assert "SIP leg was created" in exc.value.detail
        assert "not_found: no worker" in exc.value.detail
        assert "agent=kwami-agent" in exc.value.detail
        assert "LIVEKIT_AGENT_NAME" in exc.value.detail
        assert "CreateDispatch failed" in caplog.text

    async def test_a_dispatch_failure_without_metadata(self, lkapi, trunks):
        lkapi.agent_dispatch.error = _twirp(code="internal", message="", metadata={})
        with pytest.raises(HTTPException) as exc:
            await create_outbound_call(
                kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
            )
        assert "unknown error" in exc.value.detail
        assert "(" not in exc.value.detail.split("Ensure")[0].split("error.")[-1]

    async def test_a_dispatch_without_an_id_yields_an_empty_string(self, lkapi, trunks):
        async def create_dispatch(request):
            return object()

        lkapi.agent_dispatch.create_dispatch = create_dispatch
        result = await create_outbound_call(
            kwami_id="k1", phone_number="+1", caller_id="+2", participant_name="Ada"
        )
        assert result["agent_dispatch_id"] == ""


def _returning(attrs: dict):
    async def _call(request):
        return type("R", (), attrs)()

    return _call


@asynccontextmanager
async def _unused():  # pragma: no cover - keeps the import used if refactored
    yield
