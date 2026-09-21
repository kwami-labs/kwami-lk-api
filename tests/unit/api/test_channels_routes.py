"""`/channels` — phone provisioning, outbound calls and outbound messages.

Twilio and LiveKit are stubbed at this module's import sites, so these cover the
orchestration: what gets written when a purchase half-succeeds, what happens on
release when a provider step fails, and — the one that matters most — that a
failed outbound call or message still records a `failed` event before the error
propagates, so an operator can see the attempt.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from src.api.routes import channels as ch
from src.core.config import settings
from tests.helpers import async_return

pytestmark = pytest.mark.anyio

# `channels.get_owned_kwami` raises a bare ValueError, so an ownership rejection
# reaches the catch-all handler as a 500 rather than a 404. Routes that wrap it
# in their own try/except (release) do return 404; the rest do not. The shared
# client fixtures re-raise app exceptions, so these use a transport that lets the
# handler produce its response instead.
LEGACY_OWNERSHIP_STATUS = 500


@pytest.fixture
async def other_tenant_client_soft(app_instance, other_tenant, auth_registry):
    from tests.conftest import TEST_USER_HEADER

    auth_registry[other_tenant.auth_user.id] = other_tenant.auth_user
    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False),
        base_url="http://test",
        headers={TEST_USER_HEADER: other_tenant.auth_user.id},
    ) as c:
        yield c


@pytest.fixture
async def tenant_client_soft(app_instance, tenant, auth_registry):
    from tests.conftest import TEST_USER_HEADER

    auth_registry[tenant.auth_user.id] = tenant.auth_user
    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False),
        base_url="http://test",
        headers={TEST_USER_HEADER: tenant.auth_user.id},
    ) as c:
        yield c


@pytest.fixture
def stub_providers(monkeypatch):
    """Replace every Twilio/LiveKit call site with a recording stub.

    All of these are coroutines now: the Twilio helpers moved to the SDK's
    `*_async` resource methods so provisioning a number or sending a message
    stops parking the event loop on an HTTPS round trip.
    """
    calls: dict[str, Any] = {
        "purchased": None,
        "released": [],
        "detached": [],
        "attached": [],
        "calls": [],
        "messages": [],
    }

    async def search_available_numbers(**kw):
        return [{"phoneNumber": "+14155552671", "capabilities": {"voice": True}}]

    async def purchase_phone_number(**kw):
        calls["purchased"] = kw
        return {"sid": "PN1", "phone_number": kw["phone_number"]}

    async def attach_phone_number_to_sip_trunk(sid):
        calls["attached"].append(sid)
        return "TP1"

    async def sync(phone):
        return {"outbound": {"configured": True, "synced": True}, "strategy": "shared_trunks"}

    async def remove(phone):
        return {"outbound": {"configured": True, "synced": True}}

    async def detach_phone_number_from_sip_trunk(sid):
        calls["detached"].append(sid)

    async def release_incoming_phone_number(sid):
        calls["released"].append(sid)

    async def outbound_call(**kw):
        calls["calls"].append(kw)
        return {
            "room_name": "kwami-call-x",
            "participant_identity": "sip_1",
            "provider_call_sid": "SCL_1",
            "agent_dispatch_id": "AD_1",
        }

    async def place_direct_pstn_test_call(**kw):
        calls["calls"].append(kw)
        return {"sid": "CA1", "status": "queued"}

    async def send_whatsapp_message(**kw):
        calls["messages"].append(kw)
        return {
            "sid": "SM1",
            "status": "queued",
            "from": kw["from_address"],
            "to": kw["to_address"],
        }

    async def send_sms_message(**kw):
        calls["messages"].append(kw)
        return {"sid": "SM2", "status": "queued", "from": kw["from_number"], "to": kw["to_number"]}

    for name, stub in [
        ("search_available_numbers", search_available_numbers),
        ("purchase_phone_number", purchase_phone_number),
        ("attach_phone_number_to_sip_trunk", attach_phone_number_to_sip_trunk),
        ("sync_shared_livekit_trunks", sync),
        ("remove_phone_from_shared_livekit_trunks", remove),
        ("detach_phone_number_from_sip_trunk", detach_phone_number_from_sip_trunk),
        ("release_incoming_phone_number", release_incoming_phone_number),
        ("create_outbound_call", outbound_call),
        ("place_direct_pstn_test_call", place_direct_pstn_test_call),
        ("send_whatsapp_message", send_whatsapp_message),
        ("send_sms_message", send_sms_message),
    ]:
        monkeypatch.setattr(ch, name, stub)
    return calls


async def _purchase(client, kwami_id, **overrides):
    body = {"kwamiId": kwami_id, "phoneNumber": "+14155552671", "countryCode": "US", **overrides}
    r = await client.post("/channels/phone/purchase", json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestGetKwamiChannels:
    async def test_it_returns_the_kwami_its_config_and_its_channels(self, tenant_client, tenant):
        r = await tenant_client.get(f"/channels/kwamis/{tenant.kwami_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["kwami"]["id"] == tenant.kwami_id
        assert body["kwami"]["runtimeConfig"]["type"] == "config"
        assert body["channels"] == []
        assert body["events"] == {"calls": [], "messages": []} or "calls" in body["events"]

    async def test_it_requires_auth(self, client, tenant):
        assert (await client.get(f"/channels/kwamis/{tenant.kwami_id}")).status_code == 401


class TestSearchPhoneNumbers:
    async def test_it_returns_inventory(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.get("/channels/phone/search", params={"kwamiId": tenant.kwami_id})
        assert r.status_code == 200
        assert r.json()["results"][0]["phoneNumber"] == "+14155552671"

    @pytest.mark.parametrize("limit", [0, 21])
    async def test_an_out_of_range_limit_is_a_422(self, tenant_client, tenant, limit):
        r = await tenant_client.get(
            "/channels/phone/search", params={"kwamiId": tenant.kwami_id, "limit": limit}
        )
        assert r.status_code == 422

    async def test_the_kwami_id_is_required(self, tenant_client):
        assert (await tenant_client.get("/channels/phone/search")).status_code == 422

    async def test_it_requires_auth(self, client, tenant):
        r = await client.get("/channels/phone/search", params={"kwamiId": tenant.kwami_id})
        assert r.status_code == 401


class TestPurchase:
    async def test_it_creates_three_channels(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        body = await _purchase(tenant_client, tenant.kwami_id, displayName="Ada Line")
        assert body["voiceChannel"]["kind"] == "voice_phone"
        assert body["whatsappChannel"]["kind"] == "whatsapp"
        assert body["smsChannel"]["kind"] == "sms"
        assert body["voiceChannel"]["status"] == "active"
        assert body["whatsappChannel"]["provider_sender"] == "whatsapp:+14155552671"
        assert body["smsChannel"]["provider_sender"] == "+14155552671"
        assert len(fake_supabase.db.rows("kwami_channels")) == 3

    async def test_the_friendly_name_falls_back_to_the_kwami_name(
        self, tenant_client, tenant, stub_providers
    ):
        await _purchase(tenant_client, tenant.kwami_id)
        assert stub_providers["purchased"]["friendly_name"].endswith("Line")

    async def test_the_number_is_normalised_before_purchase(
        self, tenant_client, tenant, stub_providers
    ):
        await _purchase(tenant_client, tenant.kwami_id, phoneNumber="(415) 555-2671")
        assert stub_providers["purchased"]["phone_number"] == "+14155552671"

    async def test_an_invalid_number_is_a_400(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/phone/purchase",
            json={"kwamiId": tenant.kwami_id, "phoneNumber": "nonsense"},
        )
        assert r.status_code == 400

    async def test_a_trunk_attach_failure_does_not_block_the_purchase(
        self, monkeypatch, tenant_client, tenant, stub_providers, caplog
    ):
        def boom(sid):
            raise RuntimeError("twilio trunk down")

        monkeypatch.setattr(ch, "attach_phone_number_to_sip_trunk", boom)
        with caplog.at_level("WARNING", logger="kwami-api.channels"):
            body = await _purchase(tenant_client, tenant.kwami_id)
        assert body["voiceChannel"]["metadata"]["twilioSipTrunkPhoneSid"] is None
        assert "Failed to attach number" in caplog.text

    async def test_a_livekit_sync_failure_marks_routing_pending(
        self, monkeypatch, tenant_client, tenant, stub_providers, caplog
    ):
        async def boom(phone):
            raise RuntimeError("livekit unreachable")

        monkeypatch.setattr(ch, "sync_shared_livekit_trunks", boom)
        with caplog.at_level("WARNING", logger="kwami-api.channels"):
            body = await _purchase(tenant_client, tenant.kwami_id)
        assert body["voiceChannel"]["status"] == "routing_pending"
        assert body["sharedInfrastructure"]["error"] == "livekit unreachable"
        assert body["voiceChannel"]["capabilities"]["outbound"] is False

    async def test_an_unsynced_outbound_trunk_marks_routing_pending(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        async def unsynced(phone):
            return {"outbound": {"configured": True, "synced": False}}

        monkeypatch.setattr(ch, "sync_shared_livekit_trunks", unsynced)
        monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", "ST_out", raising=False)
        body = await _purchase(tenant_client, tenant.kwami_id)
        assert body["voiceChannel"]["status"] == "routing_pending"

    async def test_a_purchase_without_a_sid_skips_provider_wiring(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        monkeypatch.setattr(ch, "purchase_phone_number", async_return({"sid": None}))
        body = await _purchase(tenant_client, tenant.kwami_id)
        assert body["sharedInfrastructure"] is None
        assert stub_providers["attached"] == []

    async def test_another_tenants_kwami_is_refused(
        self, other_tenant_client_soft, tenant, stub_providers, fake_supabase
    ):
        """Denied -- but as a 500, via the legacy bare-ValueError ownership check."""
        r = await other_tenant_client_soft.post(
            "/channels/phone/purchase",
            json={"kwamiId": tenant.kwami_id, "phoneNumber": "+14155552671"},
        )
        assert r.status_code == LEGACY_OWNERSHIP_STATUS
        assert fake_supabase.db.rows("kwami_channels") == [], "nothing was provisioned"


class TestRelease:
    async def _setup(self, client, tenant, stub_providers):
        body = await _purchase(client, tenant.kwami_id)
        return body["voiceChannel"]["id"]

    async def test_it_removes_every_channel_sharing_the_number(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        channel_id = await self._setup(tenant_client, tenant, stub_providers)
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": tenant.kwami_id, "channelId": channel_id},
        )
        assert r.status_code == 200
        assert len(r.json()["removedChannelIds"]) == 3
        assert fake_supabase.db.rows("kwami_channels") == []
        assert stub_providers["released"] == ["PN1"]
        assert stub_providers["detached"] == ["TP1"]

    async def test_provider_resources_can_be_kept(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        channel_id = await self._setup(tenant_client, tenant, stub_providers)
        r = await tenant_client.post(
            "/channels/phone/release",
            json={
                "kwamiId": tenant.kwami_id,
                "channelId": channel_id,
                "releaseProviderResources": False,
            },
        )
        assert r.status_code == 200
        assert stub_providers["released"] == []
        assert fake_supabase.db.rows("kwami_channels") == []

    async def test_an_unknown_kwami_is_a_404(self, tenant_client, stub_providers):
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": "00000000-0000-0000-0000-000000000000", "channelId": "c1"},
        )
        assert r.status_code == 404

    async def test_an_unknown_channel_is_a_404(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": tenant.kwami_id, "channelId": "00000000-0000-0000-0000-000000000000"},
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "Channel not found"

    async def test_a_channel_from_another_kwami_is_a_400(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        channel_id = await self._setup(tenant_client, tenant, stub_providers)
        second = fake_supabase.db.seed(
            "user_kwamis", {"user_id": tenant.user_id, "name": "Second", "config": {}}
        )[0]
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": str(second["id"]), "channelId": channel_id},
        )
        assert r.status_code == 400
        assert "does not belong" in r.json()["detail"]

    async def test_a_livekit_cleanup_failure_does_not_block_release(
        self, monkeypatch, tenant_client, tenant, stub_providers, caplog
    ):
        channel_id = await self._setup(tenant_client, tenant, stub_providers)

        async def boom(phone):
            raise RuntimeError("livekit down")

        monkeypatch.setattr(ch, "remove_phone_from_shared_livekit_trunks", boom)
        with caplog.at_level("WARNING", logger="kwami-api.channels"):
            r = await tenant_client.post(
                "/channels/phone/release",
                json={"kwamiId": tenant.kwami_id, "channelId": channel_id},
            )
        assert r.status_code == 200
        assert r.json()["provider"]["livekit"] == {"error": "livekit down"}

    async def test_a_trunk_detach_failure_does_not_block_release(
        self, monkeypatch, tenant_client, tenant, stub_providers, caplog
    ):
        channel_id = await self._setup(tenant_client, tenant, stub_providers)

        def boom(sid):
            raise RuntimeError("detach failed")

        monkeypatch.setattr(ch, "detach_phone_number_from_sip_trunk", boom)
        with caplog.at_level("WARNING", logger="kwami-api.channels"):
            r = await tenant_client.post(
                "/channels/phone/release",
                json={"kwamiId": tenant.kwami_id, "channelId": channel_id},
            )
        assert r.status_code == 200
        assert r.json()["provider"]["twilioTrunk"] == {"error": "detach failed"}

    async def test_a_number_release_failure_is_a_502_and_keeps_the_rows(
        self, monkeypatch, tenant_client, tenant, stub_providers, fake_supabase, caplog
    ):
        """Releasing the rows while Twilio still bills for the number is worse."""
        channel_id = await self._setup(tenant_client, tenant, stub_providers)

        def boom(sid):
            raise RuntimeError("twilio refused")

        monkeypatch.setattr(ch, "release_incoming_phone_number", boom)
        with caplog.at_level("WARNING", logger="kwami-api.channels"):
            r = await tenant_client.post(
                "/channels/phone/release",
                json={"kwamiId": tenant.kwami_id, "channelId": channel_id},
            )
        assert r.status_code == 502
        assert "Could not release phone number in Twilio" in r.json()["detail"]
        assert len(fake_supabase.db.rows("kwami_channels")) == 3

    async def test_a_channel_with_no_incoming_sid_releases_only_itself(
        self, monkeypatch, tenant_client, tenant, stub_providers, fake_supabase
    ):
        monkeypatch.setattr(ch, "purchase_phone_number", async_return({"sid": ""}))
        body = await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": tenant.kwami_id, "channelId": body["voiceChannel"]["id"]},
        )
        assert r.status_code == 200
        assert len(r.json()["removedChannelIds"]) == 1
        assert r.json()["provider"]["twilioIncoming"] is None

    async def test_a_number_with_no_voice_channel_finds_no_trunk_sid(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        """The loop can run to completion without ever seeing a voice_phone row."""
        body = await _purchase(tenant_client, tenant.kwami_id)
        rows = fake_supabase.db.rows("kwami_channels")
        rows[:] = [r for r in rows if r["kind"] != "voice_phone"]
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": tenant.kwami_id, "channelId": body["smsChannel"]["id"]},
        )
        assert r.status_code == 200
        assert r.json()["provider"]["twilioTrunk"] == "skipped"
        assert len(r.json()["removedChannelIds"]) == 2

    async def test_a_non_voice_channel_is_skipped_when_looking_for_the_trunk_sid(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        """The loop scans past sms/whatsapp rows to find the voice one."""
        body = await _purchase(tenant_client, tenant.kwami_id)
        # Put the voice row last so the loop has to skip the other two.
        rows = fake_supabase.db.rows("kwami_channels")
        rows.sort(key=lambda r: r["kind"] == "voice_phone")
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": tenant.kwami_id, "channelId": body["voiceChannel"]["id"]},
        )
        assert r.status_code == 200
        assert r.json()["provider"]["twilioTrunk"] == "detached"

    async def test_a_voice_channel_with_non_dict_metadata_is_tolerated(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        body = await _purchase(tenant_client, tenant.kwami_id)
        for row in fake_supabase.db.rows("kwami_channels"):
            if row["kind"] == "voice_phone":
                row["metadata"] = None
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": tenant.kwami_id, "channelId": body["voiceChannel"]["id"]},
        )
        assert r.status_code == 200
        assert r.json()["provider"]["twilioTrunk"] == "skipped"

    async def test_a_voice_channel_without_a_trunk_sid_is_skipped(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        monkeypatch.setattr(ch, "attach_phone_number_to_sip_trunk", lambda sid: None)
        body = await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/phone/release",
            json={"kwamiId": tenant.kwami_id, "channelId": body["voiceChannel"]["id"]},
        )
        assert r.json()["provider"]["twilioTrunk"] == "skipped"


class TestConfigureWhatsapp:
    async def test_it_updates_the_sender(self, tenant_client, tenant, stub_providers):
        body = await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/whatsapp/configure",
            json={
                "channelId": body["whatsappChannel"]["id"],
                "status": "sandbox",
                "providerSender": "whatsapp:+15550000000",
                "metadata": {"note": "manual"},
            },
        )
        assert r.status_code == 200
        channel = r.json()["channel"]
        assert channel["status"] == "sandbox"
        assert channel["provider_sender"] == "whatsapp:+15550000000"
        assert channel["metadata"]["note"] == "manual"

    async def test_omitted_fields_keep_their_values(self, tenant_client, tenant, stub_providers):
        body = await _purchase(tenant_client, tenant.kwami_id)
        before = body["whatsappChannel"]
        r = await tenant_client.post(
            "/channels/whatsapp/configure", json={"channelId": before["id"]}
        )
        assert r.json()["channel"]["status"] == before["status"]
        assert r.json()["channel"]["provider_sender"] == before["provider_sender"]

    async def test_a_non_whatsapp_channel_is_a_400(self, tenant_client, tenant, stub_providers):
        body = await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/whatsapp/configure",
            json={"channelId": body["voiceChannel"]["id"]},
        )
        assert r.status_code == 400
        assert "not a WhatsApp sender" in r.json()["detail"]


class TestOutboundCall:
    async def _channel(self, client, tenant, stub_providers):
        return (await _purchase(client, tenant.kwami_id))["voiceChannel"]

    async def test_it_places_a_call_and_records_the_event(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        await self._channel(tenant_client, tenant, stub_providers)
        r = await tenant_client.post(
            "/channels/calls/outbound",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 200
        assert r.json()["call"]["status"] == "queued"
        assert r.json()["call"]["provider_call_sid"] == "SCL_1"
        assert len(fake_supabase.db.rows("kwami_call_events")) == 1

    async def test_an_explicit_channel_id_is_used(self, tenant_client, tenant, stub_providers):
        channel = await self._channel(tenant_client, tenant, stub_providers)
        r = await tenant_client.post(
            "/channels/calls/outbound",
            json={
                "kwamiId": tenant.kwami_id,
                "toNumber": "+14155559999",
                "channelId": channel["id"],
            },
        )
        assert r.status_code == 200

    async def test_no_channel_configured_is_a_404(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/calls/outbound",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 404
        assert "No phone channel configured" in r.json()["detail"]

    async def test_an_invalid_number_is_a_400(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/calls/outbound",
            json={"kwamiId": tenant.kwami_id, "toNumber": "nonsense"},
        )
        assert r.status_code == 400

    async def test_the_value_error_arm_is_unreachable_dead_code(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        """Same dead arm as `/contacts`: normalize raises InvalidPhoneNumberError.

        That is a DomainError, not a ValueError, so this `except` never fires --
        the 400 a bad number produces comes from the domain-error handler. Forced
        so the branch is exercised; the arm should be deleted.
        """

        def raise_plain_value_error(value, region):
            raise ValueError("a plain ValueError, which production never raises")

        monkeypatch.setattr(ch, "normalize_phone_number", raise_plain_value_error)
        r = await tenant_client.post(
            "/channels/calls/outbound",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 400
        assert "a plain ValueError" in r.json()["detail"]

    async def test_a_failure_records_a_failed_event_before_propagating(
        self, monkeypatch, tenant_client, tenant_client_soft, tenant, stub_providers, fake_supabase
    ):
        """Without this the attempt vanishes and nobody can see the call was tried."""
        await self._channel(tenant_client, tenant, stub_providers)

        async def boom(**kw):
            raise RuntimeError("livekit exploded")

        monkeypatch.setattr(ch, "create_outbound_call", boom)
        r = await tenant_client_soft.post(
            "/channels/calls/outbound",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 500
        (event,) = fake_supabase.db.rows("kwami_call_events")
        assert event["status"] == "failed"
        assert event["error_message"] == "livekit exploded"

    async def test_an_http_exception_is_re_raised_unchanged(
        self, monkeypatch, tenant_client, tenant, stub_providers, fake_supabase
    ):
        await self._channel(tenant_client, tenant, stub_providers)

        async def boom(**kw):
            raise HTTPException(status_code=502, detail="LiveKit said no")

        monkeypatch.setattr(ch, "create_outbound_call", boom)
        r = await tenant_client.post(
            "/channels/calls/outbound",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 502
        assert r.json()["detail"] == "LiveKit said no"
        assert fake_supabase.db.rows("kwami_call_events") == [], "no failed row for a clean 502"


class TestTwilioDirectCall:
    async def test_it_places_a_direct_call(self, tenant_client, tenant, stub_providers):
        await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/calls/twilio-direct",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 200
        assert r.json()["call"]["provider_call_sid"] == "CA1"
        assert r.json()["call"]["provider_payload"]["mode"] == "twilio_direct"

    async def test_no_channel_is_a_404(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/calls/twilio-direct",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 404

    async def test_an_invalid_number_is_a_400(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/calls/twilio-direct",
            json={"kwamiId": tenant.kwami_id, "toNumber": "nonsense"},
        )
        assert r.status_code == 400

    async def test_the_value_error_arm_is_unreachable_dead_code(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        """The same dead arm as the LiveKit call route above."""

        def raise_plain_value_error(value, region):
            raise ValueError("a plain ValueError, which production never raises")

        monkeypatch.setattr(ch, "normalize_phone_number", raise_plain_value_error)
        r = await tenant_client.post(
            "/channels/calls/twilio-direct",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 400
        assert "a plain ValueError" in r.json()["detail"]

    async def test_an_explicit_channel_id_is_used(self, tenant_client, tenant, stub_providers):
        body = await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/calls/twilio-direct",
            json={
                "kwamiId": tenant.kwami_id,
                "toNumber": "+14155559999",
                "channelId": body["voiceChannel"]["id"],
            },
        )
        assert r.status_code == 200

    async def test_a_failure_records_the_attempt_and_is_a_502(
        self, monkeypatch, tenant_client, tenant, stub_providers, fake_supabase
    ):
        await _purchase(tenant_client, tenant.kwami_id)

        def boom(**kw):
            raise RuntimeError("twilio refused")

        monkeypatch.setattr(ch, "place_direct_pstn_test_call", boom)
        r = await tenant_client.post(
            "/channels/calls/twilio-direct",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 502
        assert r.json()["detail"] == "twilio refused"
        (event,) = fake_supabase.db.rows("kwami_call_events")
        assert event["status"] == "failed"

    async def test_an_http_exception_is_re_raised_unchanged(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        await _purchase(tenant_client, tenant.kwami_id)

        def boom(**kw):
            raise HTTPException(status_code=503, detail="not configured")

        monkeypatch.setattr(ch, "place_direct_pstn_test_call", boom)
        r = await tenant_client.post(
            "/channels/calls/twilio-direct",
            json={"kwamiId": tenant.kwami_id, "toNumber": "+14155559999"},
        )
        assert r.status_code == 503


class TestOutboundMessage:
    def _body(self, kwami_id, **overrides):
        return {"kwamiId": kwami_id, "toNumber": "+14155559999", "body": "hello", **overrides}

    async def test_a_whatsapp_message(self, tenant_client, tenant, stub_providers, fake_supabase):
        await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/messages/outbound", json=self._body(tenant.kwami_id)
        )
        assert r.status_code == 200
        assert r.json()["message"]["provider_message_sid"] == "SM1"
        assert stub_providers["messages"][0]["to_address"] == "whatsapp:+14155559999"

    async def test_an_sms_message(self, tenant_client, tenant, stub_providers):
        await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelKind="sms"),
        )
        assert r.status_code == 200
        assert r.json()["message"]["provider_message_sid"] == "SM2"
        assert stub_providers["messages"][0]["to_number"] == "+14155559999"

    @pytest.mark.parametrize("body", ["", "   "])
    async def test_an_empty_body_is_a_400(self, tenant_client, tenant, body):
        r = await tenant_client.post(
            "/channels/messages/outbound", json=self._body(tenant.kwami_id, body=body)
        )
        assert r.status_code == 400
        assert "Message body is required" in r.json()["detail"]

    async def test_an_unknown_channel_kind_is_a_400(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelKind="carrier-pigeon"),
        )
        assert r.status_code == 400
        assert "channelKind must be" in r.json()["detail"]

    async def test_the_kind_is_normalised(self, tenant_client, tenant, stub_providers):
        await _purchase(tenant_client, tenant.kwami_id)
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelKind="  SMS  "),
        )
        assert r.status_code == 200

    async def test_no_whatsapp_channel_is_a_404(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/messages/outbound", json=self._body(tenant.kwami_id)
        )
        assert r.status_code == 404
        assert "No WhatsApp channel" in r.json()["detail"]

    async def test_no_sms_channel_is_a_404(self, tenant_client, tenant, stub_providers):
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelKind="sms"),
        )
        assert r.status_code == 404
        assert "No SMS channel" in r.json()["detail"]

    async def test_a_bare_e164_sender_is_coerced_to_whatsapp_form(
        self, monkeypatch, tenant_client, tenant, stub_providers, fake_supabase
    ):
        """Config often holds a plain number; Twilio needs the `whatsapp:` prefix."""
        body = await _purchase(tenant_client, tenant.kwami_id)
        for row in fake_supabase.db.rows("kwami_channels"):
            if row["kind"] == "whatsapp":
                row["provider_sender"] = "+14155552671"
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelId=body["whatsappChannel"]["id"]),
        )
        assert r.status_code == 200
        assert stub_providers["messages"][0]["from_address"] == "whatsapp:+14155552671"

    async def test_no_whatsapp_sender_at_all_is_a_503(
        self, monkeypatch, tenant_client, tenant, stub_providers, fake_supabase
    ):
        body = await _purchase(tenant_client, tenant.kwami_id)
        for row in fake_supabase.db.rows("kwami_channels"):
            if row["kind"] == "whatsapp":
                row["provider_sender"] = ""
        monkeypatch.setattr(settings, "twilio_whatsapp_from", None, raising=False)
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelId=body["whatsappChannel"]["id"]),
        )
        assert r.status_code == 503
        assert "No WhatsApp sender is configured" in r.json()["detail"]

    async def test_no_sms_sender_is_a_503(
        self, tenant_client, tenant, stub_providers, fake_supabase
    ):
        body = await _purchase(tenant_client, tenant.kwami_id)
        sms_id = body["smsChannel"]["id"]
        for row in fake_supabase.db.rows("kwami_channels"):
            if row["kind"] == "sms":
                row["phone_number"] = ""
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelKind="sms", channelId=sms_id),
        )
        assert r.status_code == 503
        assert "No SMS sender is configured" in r.json()["detail"]

    @pytest.mark.parametrize("status", ["pending", "disabled"])
    async def test_an_unready_whatsapp_sender_is_a_409(
        self, tenant_client, tenant, stub_providers, fake_supabase, status
    ):
        body = await _purchase(tenant_client, tenant.kwami_id)
        for row in fake_supabase.db.rows("kwami_channels"):
            if row["kind"] == "whatsapp":
                row["status"] = status
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelId=body["whatsappChannel"]["id"]),
        )
        assert r.status_code == 409
        assert "not ready" in r.json()["detail"]

    @pytest.mark.parametrize("status", ["active", "sandbox"])
    async def test_a_ready_whatsapp_sender_sends(
        self, tenant_client, tenant, stub_providers, fake_supabase, status
    ):
        body = await _purchase(tenant_client, tenant.kwami_id)
        for row in fake_supabase.db.rows("kwami_channels"):
            if row["kind"] == "whatsapp":
                row["status"] = status
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelId=body["whatsappChannel"]["id"]),
        )
        assert r.status_code == 200

    async def test_a_send_failure_records_the_attempt_and_is_a_502(
        self, monkeypatch, tenant_client, tenant, stub_providers, fake_supabase
    ):
        await _purchase(tenant_client, tenant.kwami_id)

        def boom(**kw):
            raise RuntimeError("twilio refused")

        monkeypatch.setattr(ch, "send_whatsapp_message", boom)
        r = await tenant_client.post(
            "/channels/messages/outbound", json=self._body(tenant.kwami_id)
        )
        assert r.status_code == 502
        (event,) = fake_supabase.db.rows("kwami_message_events")
        assert event["provider_status"] == "failed"
        assert event["error_message"] == "twilio refused"

    async def test_an_unapproved_whatsapp_sender_is_a_409(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        """Twilio 63007 means the sender is not registered -- that is a 409, not a 502."""
        await _purchase(tenant_client, tenant.kwami_id)

        def boom(**kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(ch, "send_whatsapp_message", boom)
        monkeypatch.setattr(ch, "extract_twilio_error", lambda exc: ("63007", "Channel not found"))
        r = await tenant_client.post(
            "/channels/messages/outbound", json=self._body(tenant.kwami_id)
        )
        assert r.status_code == 409
        assert "not available in Twilio" in r.json()["detail"]

    async def test_an_sms_failure_is_a_502(
        self, monkeypatch, tenant_client, tenant, stub_providers
    ):
        await _purchase(tenant_client, tenant.kwami_id)

        def boom(**kw):
            raise RuntimeError("twilio refused")

        monkeypatch.setattr(ch, "send_sms_message", boom)
        r = await tenant_client.post(
            "/channels/messages/outbound",
            json=self._body(tenant.kwami_id, channelKind="sms"),
        )
        assert r.status_code == 502

    async def test_it_requires_auth(self, client, tenant):
        r = await client.post("/channels/messages/outbound", json=self._body(tenant.kwami_id))
        assert r.status_code == 401
