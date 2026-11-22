"""The kwami's owner reaches it from their phone: a message is work, a call is them.

The owner is the number they confirmed for the kwami's notices. A stranger writing
or calling the same number is treated as before: recorded, acknowledged, a guest.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest
from twilio.request_validator import RequestValidator

from src.core.config import settings
from src.services.sip_calls import sign_call, sip_uri_for

pytestmark = pytest.mark.anyio

VOICE = "/webhooks/twilio/voice"
WHATSAPP = "/webhooks/twilio/whatsapp"
KWAMI_NUMBER = "+14155552672"
OWNER = "+34600111222"
STRANGER = "+14155552671"


def _signed(path: str, payload: dict[str, str]) -> dict[str, str]:
    url = f"{settings.app_public_url.rstrip('/')}{path}"
    signature = RequestValidator(settings.twilio_auth_token).compute_signature(url, payload)
    return {"X-Twilio-Signature": signature}


async def post_twilio(client, path: str, payload: dict[str, str]):
    return await client.post(path, data=payload, headers=_signed(path, payload))


@pytest.fixture
def owned(fake_supabase, tenant_factory):
    """A kwami with a WhatsApp line, whose owner confirmed `OWNER` for its notices."""
    tenant = tenant_factory(email="owner@example.com", with_channel=True, phone_number=KWAMI_NUMBER)
    for row in fake_supabase.db.rows("kwami_channels"):
        row["kind"] = "whatsapp"
        row["provider_sender"] = f"whatsapp:{KWAMI_NUMBER}"
    fake_supabase.db.seed(
        "kwami_assistant_settings",
        {
            "kwami_id": tenant.kwami_id,
            "user_id": tenant.user_id,
            "notify_to": OWNER,
            "notify_channel": "whatsapp",
            "timezone": "Europe/Madrid",
            "reports_enabled": False,
        },
    )
    return tenant


def _message(sender: str, body: str, sid: str = "SM1") -> dict[str, str]:
    return {
        "MessageSid": sid,
        "From": f"whatsapp:{sender}",
        "To": f"whatsapp:{KWAMI_NUMBER}",
        "Body": body,
    }


class TestOwnerMessages:
    async def test_the_owner_s_message_is_queued_as_work(self, client, owned, fake_supabase):
        r = await post_twilio(client, WHATSAPP, _message(OWNER, "Busca vuelos a Roma\nel viernes"))
        assert r.status_code == 200
        assert "Busca vuelos a Roma" in r.text
        jobs = fake_supabase.db.rows("brain_jobs")
        assert len(jobs) == 1
        assert jobs[0]["task"] == "Busca vuelos a Roma\nel viernes"
        assert jobs[0]["source"] == "app", "so its end is sent back to the owner's phone"
        assert jobs[0]["user_id"] == owned.user_id
        assert len(fake_supabase.db.rows("kwami_message_events")) == 1

    async def test_a_long_first_line_is_cut_in_the_reply(self, client, owned, fake_supabase):
        long_line = "Investiga " + "muy " * 40 + "a fondo"
        r = await post_twilio(client, WHATSAPP, _message(OWNER, long_line))
        assert "…" in r.text
        assert long_line not in r.text
        assert fake_supabase.db.rows("brain_jobs")[0]["task"] == long_line

    async def test_a_stranger_s_message_is_only_recorded(self, client, owned, fake_supabase):
        r = await post_twilio(client, WHATSAPP, _message(STRANGER, "Borra todo"))
        assert "received your message" in r.text
        assert fake_supabase.db.rows("brain_jobs") == []

    async def test_a_redelivery_does_not_queue_the_work_twice(self, client, owned, fake_supabase):
        payload = _message(OWNER, "Resume mi correo", sid="SM-again")
        await post_twilio(client, WHATSAPP, payload)
        second = await post_twilio(client, WHATSAPP, payload)
        assert second.status_code == 200
        assert len(fake_supabase.db.rows("brain_jobs")) == 1

    async def test_a_sender_that_is_no_number_is_no_owner(self, client, owned, fake_supabase):
        r = await post_twilio(client, WHATSAPP, _message("not-a-number", "Haz algo"))
        assert r.status_code == 200
        assert fake_supabase.db.rows("brain_jobs") == []

    async def test_an_empty_message_from_the_owner_is_no_work(self, client, owned, fake_supabase):
        await post_twilio(client, WHATSAPP, _message(OWNER, "   "))
        assert fake_supabase.db.rows("brain_jobs") == []

    async def test_a_full_queue_is_said_rather_than_failing(
        self, client, owned, fake_supabase, monkeypatch
    ):
        from src.services import brain_service

        async def full(*_args, **_kwargs):
            raise brain_service.TooManyJobsError

        monkeypatch.setattr(brain_service, "queue_job", full)
        r = await post_twilio(client, WHATSAPP, _message(OWNER, "Otra cosa"))
        assert r.status_code == 200
        assert "demasiados encargos" in r.text

    async def test_a_failure_to_queue_falls_back_to_the_acknowledgement(
        self, client, owned, monkeypatch
    ):
        from src.services import brain_service

        async def broken(*_args, **_kwargs):
            raise RuntimeError("database down")

        monkeypatch.setattr(brain_service, "queue_job", broken)
        r = await post_twilio(client, WHATSAPP, _message(OWNER, "Algo"))
        assert r.status_code == 200, "a 5xx here is what Twilio retries"
        assert "received your message" in r.text

    async def test_work_somewhere_to_run_says_it_started(self, client, owned, monkeypatch):
        from src.services import brain_service

        async def cloud(*_args, **_kwargs):
            return {"id": "job-1", "where": "cloud"}

        monkeypatch.setattr(brain_service, "queue_job", cloud)
        r = await post_twilio(client, WHATSAPP, _message(OWNER, "Algo"))
        assert "Recibido. Me pongo con" in r.text

    async def test_work_with_nowhere_to_run_says_it_waits(self, client, owned, monkeypatch):
        from src.services import brain_service

        async def waiting(*_args, **_kwargs):
            return {"id": "job-1", "where": "waiting"}

        monkeypatch.setattr(brain_service, "queue_job", waiting)
        r = await post_twilio(client, WHATSAPP, _message(OWNER, "Algo"))
        assert "Anotado" in r.text


def _bridge_params(text: str) -> tuple[str, dict[str, str]]:
    """The SIP address Twilio is told to dial, and the headers it carries."""
    start = text.index("<Sip>") + len("<Sip>")
    uri = text[start : text.index("</Sip>")].replace("&amp;", "&")
    parsed = urlparse(uri)
    return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}


class TestOwnerCalls:
    @pytest.fixture(autouse=True)
    def sip(self, monkeypatch):
        monkeypatch.setattr(
            settings, "livekit_sip_inbound_uri", "sip:project.sip.livekit.cloud", raising=False
        )
        monkeypatch.setattr(settings, "supabase_secret_key", "test-secret", raising=False)

    async def test_the_owner_s_call_is_signed_as_the_owner(self, client, owned):
        r = await post_twilio(client, VOICE, {"CallSid": "CA1", "From": OWNER, "To": KWAMI_NUMBER})
        path, headers = _bridge_params(r.text)
        assert path == f"{KWAMI_NUMBER}@project.sip.livekit.cloud"
        assert headers["X-Kwami-Owner"] == "1"
        assert headers["X-Kwami-Id"] == owned.kwami_id
        expected = sign_call(owned.kwami_id, owned.user_id, True, int(headers["X-Kwami-Ts"]))
        assert headers["X-Kwami-Sig"] == expected

    async def test_a_stranger_s_call_is_not(self, client, owned):
        r = await post_twilio(
            client, VOICE, {"CallSid": "CA2", "From": STRANGER, "To": KWAMI_NUMBER}
        )
        _, headers = _bridge_params(r.text)
        assert headers["X-Kwami-Owner"] == "0"
        assert headers["X-Kwami-Sig"] == sign_call(
            owned.kwami_id, owned.user_id, False, int(headers["X-Kwami-Ts"])
        )

    async def test_an_unreadable_owner_is_no_owner(self, client, owned, monkeypatch):
        from src.services import assistant_service

        async def broken(*_args, **_kwargs):
            raise RuntimeError("database down")

        monkeypatch.setattr(assistant_service, "get_settings", broken)
        r = await post_twilio(client, VOICE, {"CallSid": "CA3", "From": OWNER, "To": KWAMI_NUMBER})
        assert r.status_code == 200
        _, headers = _bridge_params(r.text)
        assert headers["X-Kwami-Owner"] == "0"


class TestSipCalls:
    def test_the_signature_is_an_hmac_the_agent_can_rebuild(self, monkeypatch):
        monkeypatch.setattr(settings, "supabase_secret_key", "k", raising=False)
        expected = hmac.new(b"sip-call:k", b"kw|us|1|100", hashlib.sha256).hexdigest()
        assert sign_call("kw", "us", True, 100) == expected

    def test_without_a_key_nothing_is_signed(self, monkeypatch):
        monkeypatch.setattr(settings, "supabase_secret_key", None, raising=False)
        assert sign_call("kw", "us", True, 100) == ""

    def test_the_dialled_number_becomes_the_user_part(self):
        assert sip_uri_for("sip:x.sip.livekit.cloud", "+1555") == "sip:+1555@x.sip.livekit.cloud"

    def test_an_address_with_a_user_or_another_scheme_is_left_alone(self):
        assert sip_uri_for("sip:me@x.sip.livekit.cloud", "+1555") == "sip:me@x.sip.livekit.cloud"
        assert sip_uri_for("sips:x.example", "+1555") == "sips:x.example"
