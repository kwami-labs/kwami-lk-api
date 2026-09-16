"""Twilio webhooks: signature enforcement, proxy-awareness, retry safety.

All four Twilio webhooks had zero tests. Three defects are pinned here:

* ``validate_twilio_request`` returned early when ``TWILIO_AUTH_TOKEN`` was unset,
  leaving the endpoints publicly writable -- a forged form created contacts,
  conversations and events inside whichever tenant owned the looked-up channel.
* It validated against ``str(request.url)``, which is ``http`` behind Fly's TLS
  terminator while Twilio signs the ``https`` URL, so validation could not pass
  in production.
* A malformed ``From``/``To`` raised out of the handler as a 500, and Twilio
  retries non-2xx responses.
"""

import pytest
from httpx import AsyncClient
from twilio.request_validator import RequestValidator

from src.core.config import settings

VOICE_PATH = "/webhooks/twilio/voice"


def sign(url: str, payload: dict[str, str], token: str | None = None) -> str:
    """A real Twilio signature, computed by Twilio's own validator."""
    return RequestValidator(token or settings.twilio_auth_token).compute_signature(url, payload)


def call_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "CallSid": "CA00000000000000000000000000000001",
        "From": "+14155552671",
        "To": "+14155552672",
        "CallStatus": "ringing",
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_unsigned_requests_are_rejected(client: AsyncClient):
    response = await client.post(VOICE_PATH, data=call_payload())
    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_forged_signatures_are_rejected(client: AsyncClient):
    response = await client.post(
        VOICE_PATH,
        data=call_payload(),
        headers={"X-Twilio-Signature": "not-a-real-signature"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_a_signature_from_the_wrong_token_is_rejected(client: AsyncClient):
    payload = call_payload()
    url = f"{settings.app_public_url.rstrip('/')}{VOICE_PATH}"
    response = await client.post(
        VOICE_PATH,
        data=payload,
        headers={"X-Twilio-Signature": sign(url, payload, token="someone-elses-token")},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_a_signature_over_the_public_url_is_accepted(client: AsyncClient):
    """Twilio signs the configured public https URL, not the internal http one."""
    payload = call_payload()
    url = f"{settings.app_public_url.rstrip('/')}{VOICE_PATH}"
    response = await client.post(
        VOICE_PATH, data=payload, headers={"X-Twilio-Signature": sign(url, payload)}
    )
    # Unknown destination number -> Reject TwiML, but the request was authenticated.
    assert response.status_code == 200
    assert "<Reject/>" in response.text


@pytest.mark.anyio
async def test_tampering_with_the_body_invalidates_the_signature(client: AsyncClient):
    payload = call_payload()
    url = f"{settings.app_public_url.rstrip('/')}{VOICE_PATH}"
    signature = sign(url, payload)

    tampered = call_payload(From="+14155559999")
    response = await client.post(
        VOICE_PATH, data=tampered, headers={"X-Twilio-Signature": signature}
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_missing_auth_token_fails_closed(client: AsyncClient, monkeypatch):
    """Unconfigured verification must refuse, not wave the request through."""
    monkeypatch.setattr(settings, "twilio_auth_token", None)
    response = await client.post(
        VOICE_PATH, data=call_payload(), headers={"X-Twilio-Signature": "anything"}
    )
    assert response.status_code == 503
    assert response.status_code != 200, "an unverified webhook must never be processed"


@pytest.mark.anyio
async def test_malformed_numbers_do_not_produce_a_retryable_error(client: AsyncClient):
    """Twilio retries any non-2xx, so a bad caller ID must not 5xx."""
    payload = call_payload(From="not-a-number", To="also-garbage")
    url = f"{settings.app_public_url.rstrip('/')}{VOICE_PATH}"

    response = await client.post(
        VOICE_PATH, data=payload, headers={"X-Twilio-Signature": sign(url, payload)}
    )

    assert response.status_code == 200, "a 5xx here turns one bad call into a retry storm"
    assert "<Reject/>" in response.text
