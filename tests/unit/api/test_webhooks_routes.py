"""`/webhooks` — the four Twilio endpoints and SendGrid Inbound Parse.

Signature enforcement is covered in test_webhooks_twilio.py; this covers what
happens *after* a request authenticates. The governing constraint is that both
providers retry any non-2xx, so every path that cannot do its job must still
answer 2xx (or valid TwiML) rather than raising — a malformed caller ID, an
unknown destination, mail addressed to nobody.
"""

from __future__ import annotations

import json

import pytest
from twilio.request_validator import RequestValidator

from src.core.config import settings

pytestmark = pytest.mark.anyio

VOICE = "/webhooks/twilio/voice"
VOICE_STATUS = "/webhooks/twilio/voice/status"
WHATSAPP = "/webhooks/twilio/whatsapp"
WHATSAPP_STATUS = "/webhooks/twilio/whatsapp/status"
EMAIL_INBOUND = "/webhooks/email/inbound"


def _signed(path: str, payload: dict[str, str]) -> dict[str, str]:
    url = f"{settings.app_public_url.rstrip('/')}{path}"
    signature = RequestValidator(settings.twilio_auth_token).compute_signature(url, payload)
    return {"X-Twilio-Signature": signature}


async def post_twilio(client, path: str, payload: dict[str, str]):
    return await client.post(path, data=payload, headers=_signed(path, payload))


@pytest.fixture
def sms_channel(fake_supabase, tenant_factory):
    """A tenant owning an SMS channel on +14155552672."""
    return tenant_factory(
        email="channel@example.com", with_channel=True, phone_number="+14155552672"
    )


class TestVoiceWebhook:
    async def test_an_unusable_destination_rejects_rather_than_500s(self, client, caplog):
        """A malformed To/Called must not raise -- Twilio retries a 5xx."""
        payload = {"CallSid": "CA1", "From": "+14155552671", "To": "not-a-number"}
        with caplog.at_level("WARNING", logger="kwami-api.webhooks"):
            r = await post_twilio(client, VOICE, payload)
        assert r.status_code == 200
        assert "<Reject/>" in r.text
        assert "no usable destination" in caplog.text

    async def test_an_empty_destination_rejects(self, client):
        r = await post_twilio(client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": ""})
        assert r.status_code == 200
        assert "<Reject/>" in r.text

    async def test_the_called_field_is_a_fallback_for_to(self, client, sms_channel):
        r = await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "Called": "+14155552672"}
        )
        assert r.status_code == 200
        assert "<Reject/>" not in r.text

    async def test_an_unknown_destination_rejects(self, client, caplog):
        payload = {"CallSid": "CA1", "From": "+14155552671", "To": "+14155559999"}
        with caplog.at_level("WARNING", logger="kwami-api.webhooks"):
            r = await post_twilio(client, VOICE, payload)
        assert "<Reject/>" in r.text
        assert "unknown number" in caplog.text

    async def test_a_known_destination_records_the_call(
        self, client, sms_channel, fake_supabase, monkeypatch
    ):
        monkeypatch.setattr(
            settings, "livekit_sip_inbound_uri", "sip:kwami@sip.livekit.cloud", raising=False
        )
        r = await post_twilio(
            client,
            VOICE,
            {
                "CallSid": "CA1",
                "From": "+14155552671",
                "To": "+14155552672",
                "CallerName": "Ada",
                "CallStatus": "ringing",
            },
        )
        assert r.status_code == 200
        assert "<Dial>" in r.text or "sip:" in r.text
        assert len(fake_supabase.db.rows("kwami_call_events")) == 1
        assert len(fake_supabase.db.rows("kwami_conversations")) == 1
        contacts = fake_supabase.db.rows("kwami_contacts")
        assert contacts[0]["display_name"] == "Ada"

    async def test_an_unconfigured_sip_uri_answers_with_a_spoken_notice(
        self, client, sms_channel, monkeypatch
    ):
        monkeypatch.setattr(settings, "livekit_sip_inbound_uri", None, raising=False)
        r = await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": "+14155552672"}
        )
        assert r.status_code == 200
        assert "<Say>" in r.text
        assert "not configured" in r.text

    async def test_an_unparseable_caller_is_stored_as_unknown(
        self, client, sms_channel, fake_supabase
    ):
        """The caller ID is whatever the network gave Twilio; store what we can."""
        r = await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "anonymous", "To": "+14155552672"}
        )
        assert r.status_code == 200
        assert fake_supabase.db.rows("kwami_contacts")[0]["phone_number"] == "unknown"

    async def test_the_caller_field_is_a_fallback_for_from(
        self, client, sms_channel, fake_supabase
    ):
        r = await post_twilio(
            client,
            VOICE,
            {"CallSid": "CA1", "Caller": "+14155552671", "To": "+14155552672"},
        )
        assert r.status_code == 200
        assert fake_supabase.db.rows("kwami_contacts")[0]["phone_number"] == "+14155552671"

    async def test_a_missing_call_status_defaults_to_ringing(
        self, client, sms_channel, fake_supabase
    ):
        await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": "+14155552672"}
        )
        assert fake_supabase.db.rows("kwami_call_events")[0]["status"] == "ringing"


class TestVoiceStatusWebhook:
    async def test_it_updates_the_call_event(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": "+14155552672"}
        )
        r = await post_twilio(
            client,
            VOICE_STATUS,
            {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "42"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        event = fake_supabase.db.rows("kwami_call_events")[0]
        assert event["status"] == "completed"
        assert event["duration_seconds"] == 42

    async def test_a_status_without_a_call_sid_is_acknowledged(self, client):
        r = await post_twilio(client, VOICE_STATUS, {"CallStatus": "completed"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    async def test_a_missing_status_defaults_to_completed(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": "+14155552672"}
        )
        await post_twilio(client, VOICE_STATUS, {"CallSid": "CA1"})
        assert fake_supabase.db.rows("kwami_call_events")[0]["status"] == "completed"

    async def test_an_absent_duration_stays_null(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": "+14155552672"}
        )
        await post_twilio(client, VOICE_STATUS, {"CallSid": "CA1", "CallStatus": "busy"})
        assert fake_supabase.db.rows("kwami_call_events")[0]["duration_seconds"] is None

    async def test_a_sip_response_code_is_preferred_as_the_error_message(
        self, client, sms_channel, fake_supabase
    ):
        await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": "+14155552672"}
        )
        await post_twilio(
            client,
            VOICE_STATUS,
            {
                "CallSid": "CA1",
                "CallStatus": "failed",
                "ErrorCode": "13224",
                "SipResponseCode": "486",
                "ErrorMessage": "Busy here",
            },
        )
        event = fake_supabase.db.rows("kwami_call_events")[0]
        assert event["error_code"] == "13224"
        assert event["error_message"] == "486"

    async def test_the_error_message_is_the_fallback(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client, VOICE, {"CallSid": "CA1", "From": "+14155552671", "To": "+14155552672"}
        )
        await post_twilio(
            client,
            VOICE_STATUS,
            {"CallSid": "CA1", "CallStatus": "failed", "ErrorMessage": "Busy here"},
        )
        assert fake_supabase.db.rows("kwami_call_events")[0]["error_message"] == "Busy here"


class TestWhatsappWebhook:
    async def test_an_unknown_sender_is_acknowledged_not_rejected(self, client, caplog):
        payload = {
            "MessageSid": "SM1",
            "From": "whatsapp:+14155552671",
            "To": "whatsapp:+14155559999",
            "Body": "hi",
        }
        with caplog.at_level("WARNING", logger="kwami-api.webhooks"):
            r = await post_twilio(client, WHATSAPP, payload)
        assert r.status_code == 200
        assert "not configured" in r.text
        assert "unknown sender" in caplog.text

    async def test_an_sms_to_a_known_number_is_recorded_and_acknowledged(
        self, client, sms_channel, fake_supabase
    ):
        r = await post_twilio(
            client,
            WHATSAPP,
            {
                "MessageSid": "SM1",
                "From": "+14155552671",
                "To": "+14155552672",
                "Body": "hello",
                "SmsStatus": "received",
            },
        )
        assert r.status_code == 200
        assert "received your SMS" in r.text
        assert len(fake_supabase.db.rows("kwami_message_events")) == 1

    async def test_a_whatsapp_message_gets_the_whatsapp_acknowledgement(
        self, client, fake_supabase, tenant_factory
    ):
        tenant = tenant_factory(
            email="wa@example.com", with_channel=True, phone_number="+14155552672"
        )
        for row in fake_supabase.db.rows("kwami_channels"):
            row["kind"] = "whatsapp"
            row["provider_sender"] = "whatsapp:+14155552672"
        r = await post_twilio(
            client,
            WHATSAPP,
            {
                "MessageSid": "SM1",
                "From": "whatsapp:+14155552671",
                "To": "whatsapp:+14155552672",
                "Body": "hi",
                "ProfileName": "Ada",
            },
        )
        assert r.status_code == 200
        assert "received your message" in r.text
        contact = fake_supabase.db.rows("kwami_contacts")[0]
        assert contact["whatsapp_address"] == "whatsapp:+14155552671"
        assert contact["display_name"] == "Ada"
        assert tenant is not None

    async def test_an_unparseable_sender_falls_back_to_the_raw_address(
        self, client, sms_channel, fake_supabase
    ):
        await post_twilio(
            client,
            WHATSAPP,
            {"MessageSid": "SM1", "From": "anonymous", "To": "+14155552672", "Body": "hi"},
        )
        assert fake_supabase.db.rows("kwami_contacts")[0]["phone_number"] == "anonymous"

    async def test_a_missing_sms_status_defaults_to_received(
        self, client, sms_channel, fake_supabase
    ):
        await post_twilio(
            client,
            WHATSAPP,
            {"MessageSid": "SM1", "From": "+14155552671", "To": "+14155552672", "Body": "hi"},
        )
        assert fake_supabase.db.rows("kwami_message_events")[0]["provider_status"] == "received"

    async def test_an_empty_body_is_accepted(self, client, sms_channel, fake_supabase):
        r = await post_twilio(
            client,
            WHATSAPP,
            {"MessageSid": "SM1", "From": "+14155552671", "To": "+14155552672"},
        )
        assert r.status_code == 200
        assert fake_supabase.db.rows("kwami_message_events")[0]["body"] == ""


class TestWhatsappStatusWebhook:
    async def test_it_updates_the_message_event(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client,
            WHATSAPP,
            {"MessageSid": "SM1", "From": "+14155552671", "To": "+14155552672", "Body": "hi"},
        )
        r = await post_twilio(
            client, WHATSAPP_STATUS, {"MessageSid": "SM1", "MessageStatus": "delivered"}
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert fake_supabase.db.rows("kwami_message_events")[0]["provider_status"] == "delivered"

    async def test_a_status_without_a_message_sid_is_acknowledged(self, client):
        r = await post_twilio(client, WHATSAPP_STATUS, {"MessageStatus": "delivered"})
        assert r.json() == {"ok": True}

    async def test_the_sms_status_field_is_a_fallback(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client,
            WHATSAPP,
            {"MessageSid": "SM1", "From": "+14155552671", "To": "+14155552672", "Body": "hi"},
        )
        await post_twilio(client, WHATSAPP_STATUS, {"MessageSid": "SM1", "SmsStatus": "sent"})
        assert fake_supabase.db.rows("kwami_message_events")[0]["provider_status"] == "sent"

    async def test_neither_status_field_defaults_to_sent(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client,
            WHATSAPP,
            {"MessageSid": "SM1", "From": "+14155552671", "To": "+14155552672", "Body": "hi"},
        )
        await post_twilio(client, WHATSAPP_STATUS, {"MessageSid": "SM1"})
        assert fake_supabase.db.rows("kwami_message_events")[0]["provider_status"] == "sent"

    async def test_an_error_is_recorded(self, client, sms_channel, fake_supabase):
        await post_twilio(
            client,
            WHATSAPP,
            {"MessageSid": "SM1", "From": "+14155552671", "To": "+14155552672", "Body": "hi"},
        )
        await post_twilio(
            client,
            WHATSAPP_STATUS,
            {
                "MessageSid": "SM1",
                "MessageStatus": "failed",
                "ErrorCode": "30008",
                "ErrorMessage": "Unknown destination",
            },
        )
        event = fake_supabase.db.rows("kwami_message_events")[0]
        assert event["error_code"] == "30008"
        assert event["error_message"] == "Unknown destination"


class TestSendgridInbound:
    @pytest.fixture(autouse=True)
    def _no_secret(self, monkeypatch):
        """No configured secret means the signature check is skipped (dev mode)."""
        from src.services import sendgrid_service

        monkeypatch.setattr(
            sendgrid_service.settings, "sendgrid_inbound_webhook_secret", None, raising=False
        )

    @pytest.fixture
    def account(self, fake_supabase, tenant):
        from src.services.email_service import activate_account

        return activate_account(user_id=tenant.user_id, kwami_id=tenant.kwami_id, username="ada")

    async def test_a_bad_signature_is_a_403(self, monkeypatch, client):
        from src.services import sendgrid_service

        monkeypatch.setattr(
            sendgrid_service.settings, "sendgrid_inbound_webhook_secret", "shh", raising=False
        )
        r = await client.post(
            EMAIL_INBOUND,
            data={"token": "t", "timestamp": "1", "signature": "wrong", "to": "ada@kwami.io"},
        )
        assert r.status_code == 403

    async def test_an_email_is_stored(self, client, account, fake_supabase):
        r = await client.post(
            EMAIL_INBOUND,
            data={
                "from": "billing@vendor.test",
                "to": "ada@kwami.io",
                "subject": "Your invoice",
                "text": "Amount due $10.00",
                "html": "<p>Amount due $10.00</p>",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        (msg,) = fake_supabase.db.rows("kwami_email_messages")
        assert msg["category"] == "bills"
        assert msg["direction"] == "inbound"

    async def test_the_envelope_is_preferred_over_the_to_header(
        self, client, account, fake_supabase
    ):
        """SendGrid's envelope carries clean addresses; the header may be decorated."""
        r = await client.post(
            EMAIL_INBOUND,
            data={
                "from": "a@b.c",
                "to": "Somebody Else <else@elsewhere.test>",
                "envelope": json.dumps({"to": ["ada@kwami.io"]}),
                "subject": "s",
                "text": "t",
            },
        )
        assert r.status_code == 200
        assert len(fake_supabase.db.rows("kwami_email_messages")) == 1

    async def test_an_envelope_with_a_single_string_address(self, client, account, fake_supabase):
        r = await client.post(
            EMAIL_INBOUND,
            data={
                "from": "a@b.c",
                "envelope": json.dumps({"to": "ada@kwami.io"}),
                "subject": "s",
                "text": "t",
            },
        )
        assert r.status_code == 200
        assert len(fake_supabase.db.rows("kwami_email_messages")) == 1

    async def test_a_malformed_envelope_falls_back_to_the_header(
        self, client, account, fake_supabase
    ):
        r = await client.post(
            EMAIL_INBOUND,
            data={
                "from": "a@b.c",
                "to": "ada@kwami.io",
                "envelope": "not json",
                "subject": "s",
                "text": "t",
            },
        )
        assert r.status_code == 200
        assert len(fake_supabase.db.rows("kwami_email_messages")) == 1

    async def test_an_address_list_is_split_and_entries_without_an_at_dropped(
        self, client, account, fake_supabase
    ):
        r = await client.post(
            EMAIL_INBOUND,
            data={
                "from": "a@b.c",
                "to": "ada@kwami.io, other@x.test",
                "cc": "cc@x.test, not-an-address",
                "subject": "s",
                "text": "t",
            },
        )
        assert r.status_code == 200
        (msg,) = fake_supabase.db.rows("kwami_email_messages")
        assert msg["to_addresses"] == ["ada@kwami.io", "other@x.test"]
        assert msg["cc_addresses"] == ["cc@x.test"], "entries without an @ are dropped"

    async def test_a_display_name_address_does_not_resolve_known_bug(
        self, client, account, fake_supabase
    ):
        """`_parse_address_list` cannot handle `Name <addr>`.

        It does `a.strip().strip("<>")`, which only removes angle brackets at the
        very ends of the token. `Ada <ada@kwami.io>` becomes `Ada <ada@kwami.io`,
        whose local part is `Ada <ada` -- so the account is never found and the
        mail is dropped. It has not bitten because SendGrid's `envelope` normally
        carries clean addresses and is preferred; this is the fallback path.
        Pinned as it behaves; the fix is a real address parser (`email.utils
        .getaddresses`).
        """
        r = await client.post(
            EMAIL_INBOUND,
            data={"from": "a@b.c", "to": "Ada <ada@kwami.io>", "subject": "s", "text": "t"},
        )
        assert r.status_code == 200, "still acknowledged, so SendGrid does not retry"
        assert fake_supabase.db.rows("kwami_email_messages") == [], "but nothing was stored"

    async def test_headers_are_parsed_into_a_dict(self, client, account, fake_supabase):
        r = await client.post(
            EMAIL_INBOUND,
            data={
                "from": "a@b.c",
                "to": "ada@kwami.io",
                "subject": "s",
                "text": "t",
                "headers": "Message-ID: <abc@vendor>\nX-Spam: no\nnot-a-header-line",
            },
        )
        assert r.status_code == 200
        (msg,) = fake_supabase.db.rows("kwami_email_messages")
        assert msg["sendgrid_message_id"] == "<abc@vendor>"
        assert msg["headers"]["X-Spam"] == "no"
        assert "not-a-header-line" not in msg["headers"]

    async def test_the_lowercase_message_id_header_is_accepted(
        self, client, account, fake_supabase
    ):
        await client.post(
            EMAIL_INBOUND,
            data={
                "from": "a@b.c",
                "to": "ada@kwami.io",
                "subject": "s",
                "text": "t",
                "headers": "Message-Id: <xyz@vendor>",
            },
        )
        assert (
            fake_supabase.db.rows("kwami_email_messages")[0]["sendgrid_message_id"]
            == "<xyz@vendor>"
        )

    async def test_mail_for_nobody_is_acknowledged_not_failed(self, client, caplog):
        """A non-2xx would make SendGrid redeliver a message we can never place."""
        with caplog.at_level("WARNING", logger="kwami-api.webhooks"):
            r = await client.post(
                EMAIL_INBOUND,
                data={"from": "a@b.c", "to": "nobody@kwami.io", "subject": "s", "text": "t"},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert "no matching account" in caplog.text

    async def test_a_completely_empty_post_is_acknowledged(self, client):
        r = await client.post(EMAIL_INBOUND, data={})
        assert r.status_code == 200
