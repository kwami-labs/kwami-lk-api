"""Twilio provisioning, webhook validation, and messaging helpers."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import HTTPException, Request
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Dial, VoiceResponse

from src.core.config import settings

logger = logging.getLogger("kwami-api.twilio")

_twilio_client: Client | None = None


def get_twilio_client() -> Client:
    global _twilio_client
    if _twilio_client is None:
        if not settings.twilio_enabled:
            raise RuntimeError("Platform phone provisioning is not configured")
        _twilio_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _twilio_client


def ensure_twilio_enabled() -> None:
    if not settings.twilio_enabled:
        raise HTTPException(
            status_code=503,
            detail="Platform phone provisioning is not configured on the server",
        )


def webhook_url(path: str, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if not settings.app_public_url:
        return None
    return f"{settings.app_public_url.rstrip('/')}{path}"


def signed_url_candidates(request: Request) -> list[str]:
    """The URLs Twilio may have signed, most trustworthy first.

    Twilio signs the exact URL configured on the number, which is derived from
    ``APP_PUBLIC_URL``. Using ``str(request.url)`` alone is wrong in production:
    Fly terminates TLS, so the scope's scheme is ``http`` while Twilio signed
    ``https`` -- meaning signature validation could never pass on the deployed
    service.

    ``app_public_url`` is tried first precisely because it is configuration
    rather than request data, so a spoofed ``Host`` header cannot influence it.
    """
    path = request.url.path
    query = request.url.query
    suffix = f"{path}?{query}" if query else path

    candidates: list[str] = []
    if settings.app_public_url:
        candidates.append(f"{settings.app_public_url.rstrip('/')}{suffix}")

    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        scheme = forwarded_proto.split(",")[0].strip()
        candidates.append(str(request.url.replace(scheme=scheme)))

    candidates.append(str(request.url))
    return candidates


async def validate_twilio_request(request: Request, form_payload: dict[str, str]) -> None:
    """Reject any request Twilio did not sign.

    This used to `return` when ``TWILIO_AUTH_TOKEN`` was unset, which left all
    four Twilio webhooks publicly writable: anyone could POST a forged form and
    create contacts, conversations and call/message events inside *any* tenant,
    because the handlers take user_id and kwami_id from the looked-up channel.
    Missing configuration now fails closed.
    """
    if not settings.twilio_auth_token:
        logger.error(
            "Twilio webhook received but TWILIO_AUTH_TOKEN is not configured; "
            "refusing rather than accepting an unverified request"
        )
        raise HTTPException(
            status_code=503,
            detail="Twilio webhook verification is not configured",
        )

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing Twilio signature")

    validator = RequestValidator(settings.twilio_auth_token)
    if not any(
        validator.validate(url, form_payload, signature) for url in signed_url_candidates(request)
    ):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature")


def _capabilities_dict(item: object) -> dict[str, bool]:
    caps = getattr(item, "capabilities", None)
    if isinstance(caps, dict):
        d = caps
    elif isinstance(caps, str):
        try:
            parsed = json.loads(caps)
            d = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            d = {}
    else:
        d = {}
    return {
        "voice": bool(d.get("voice", False)),
        "sms": bool(d.get("SMS", False) or d.get("sms", False)),
        "mms": bool(d.get("MMS", False) or d.get("mms", False)),
    }


def _twilio_page_is_404(exc: TwilioException) -> bool:
    if len(exc.args) >= 2:
        resp = exc.args[1]
        if getattr(resp, "status_code", None) == 404:
            return True
    return " 404 " in f" {exc} " or '"status":404' in str(exc)


def _number_search_kw(
    *,
    country_code: str,
    kind: str,
    area_code: str | None,
    contains: str | None,
    limit: int,
) -> dict:
    """Build list() kwargs; Twilio Mobile `area_code` applies only to US and CA."""
    cc = country_code.upper()
    us_ca = cc in {"US", "CA"}
    out: dict = {"limit": limit}
    if contains:
        out["contains"] = contains
    if kind in {"local", "mobile"} and us_ca and area_code:
        out["area_code"] = area_code
    return out


def search_available_numbers(
    *,
    country_code: str,
    area_code: str | None,
    contains: str | None,
    limit: int,
) -> list[dict[str, str]]:
    """Search Twilio inventory. Many countries (e.g. ES) have no Local subresource — try Mobile/TollFree."""
    ensure_twilio_enabled()
    client = get_twilio_client()
    cc = country_code.upper()
    country = client.available_phone_numbers(cc)

    kinds: tuple[str, ...] = ("local", "mobile", "toll_free")
    last_404: TwilioException | None = None

    for kind in kinds:
        sub = getattr(country, kind, None)
        if sub is None:
            continue
        kwargs = _number_search_kw(
            country_code=cc,
            kind=kind,
            area_code=area_code,
            contains=contains,
            limit=limit,
        )
        try:
            items = sub.list(**kwargs)
        except TwilioException as exc:
            if _twilio_page_is_404(exc):
                last_404 = exc
                logger.debug(
                    "Twilio %s inventory not available for %s: %s",
                    kind,
                    cc,
                    exc,
                )
                continue
            raise

        return [
            {
                "phoneNumber": item.phone_number,
                "friendlyName": item.friendly_name,
                "region": item.region,
                "locality": item.locality,
                "postalCode": item.postal_code,
                "capabilities": _capabilities_dict(item),
            }
            for item in items
        ]

    if last_404 is not None:
        logger.warning("No Twilio phone number subresources responded for country %s", cc)
    return []


def purchase_phone_number(
    *,
    phone_number: str,
    friendly_name: str,
) -> dict[str, str | None]:
    ensure_twilio_enabled()
    client = get_twilio_client()
    # Current twilio-python IncomingPhoneNumbers.create has no sms_status_callback; voice uses StatusCallback.
    incoming = client.incoming_phone_numbers.create(
        phone_number=phone_number,
        friendly_name=friendly_name,
        voice_url=webhook_url("/webhooks/twilio/voice"),
        voice_method="POST",
        status_callback=webhook_url(
            "/webhooks/twilio/voice/status",
            explicit=settings.twilio_voice_status_callback_url,
        ),
        status_callback_method="POST",
        sms_url=webhook_url("/webhooks/twilio/whatsapp"),
        sms_method="POST",
    )
    return {
        "sid": incoming.sid,
        "phone_number": incoming.phone_number,
        "friendly_name": incoming.friendly_name,
        "status": getattr(incoming, "status", None),
    }


def detach_phone_number_from_sip_trunk(trunk_phone_resource_sid: str) -> None:
    """Remove a phone association from the shared Twilio Elastic SIP trunk."""
    if not settings.twilio_sip_trunk_sid or not trunk_phone_resource_sid:
        return
    ensure_twilio_enabled()
    client = get_twilio_client()
    try:
        client.trunking.v1.trunks(settings.twilio_sip_trunk_sid).phone_numbers(
            trunk_phone_resource_sid
        ).delete()
    except TwilioRestException as exc:
        if getattr(exc, "status", None) != 404:
            raise
        logger.debug("Trunk phone association already gone: %s", trunk_phone_resource_sid)


def release_incoming_phone_number(incoming_phone_sid: str) -> None:
    """Release (delete) a Twilio IncomingPhoneNumber from the account."""
    if not incoming_phone_sid:
        raise ValueError("Missing Twilio incoming phone SID")
    ensure_twilio_enabled()
    client = get_twilio_client()
    try:
        client.incoming_phone_numbers(incoming_phone_sid).delete()
    except TwilioRestException as exc:
        if getattr(exc, "status", None) == 404:
            logger.info("Incoming phone %s already released in Twilio", incoming_phone_sid)
            return
        raise


def attach_phone_number_to_sip_trunk(phone_number_sid: str) -> str | None:
    """Attach a purchased number to the shared Twilio SIP trunk."""
    if not settings.twilio_sip_trunk_sid:
        return None

    ensure_twilio_enabled()
    client = get_twilio_client()
    resource = client.trunking.v1.trunks(settings.twilio_sip_trunk_sid).phone_numbers.create(
        phone_number_sid=phone_number_sid
    )
    return resource.sid


def place_direct_pstn_test_call(*, to_e164: str, from_e164: str) -> dict[str, str | None]:
    """Outbound PSTN call via Twilio REST only (no LiveKit SIP). Used to isolate Twilio vs LiveKit issues."""
    ensure_twilio_enabled()
    client = get_twilio_client()
    response = VoiceResponse()
    response.say(
        "This is a direct test call from your Twilio line, not using LiveKit or the Kwami agent.",
        voice="Polly.Joanna",
    )
    response.hangup()
    call = client.calls.create(to=to_e164, from_=from_e164, twiml=str(response))
    return {"sid": call.sid, "status": getattr(call, "status", None)}


def send_whatsapp_message(
    *,
    from_address: str,
    to_address: str,
    body: str,
) -> dict[str, str | None]:
    ensure_twilio_enabled()
    client = get_twilio_client()
    message = client.messages.create(
        from_=from_address,
        to=to_address,
        body=body,
        status_callback=webhook_url(
            "/webhooks/twilio/whatsapp/status",
            explicit=settings.twilio_messaging_status_callback_url,
        ),
    )
    return {
        "sid": message.sid,
        "status": message.status,
        "from": message.from_,
        "to": message.to,
    }


def send_sms_message(
    *,
    from_number: str,
    to_number: str,
    body: str,
) -> dict[str, str | None]:
    ensure_twilio_enabled()
    client = get_twilio_client()
    message = client.messages.create(
        from_=from_number,
        to=to_number,
        body=body,
        status_callback=webhook_url(
            "/webhooks/twilio/whatsapp/status",
            explicit=settings.twilio_messaging_status_callback_url,
        ),
    )
    return {
        "sid": message.sid,
        "status": message.status,
        "from": message.from_,
        "to": message.to,
    }


def build_voice_bridge_response(*, livekit_uri: str, headers: dict[str, str]) -> str:
    """Return TwiML that bridges the PSTN call into LiveKit SIP."""
    response = VoiceResponse()
    dial = Dial(answer_on_bridge=True)
    dial.sip(_append_sip_headers(livekit_uri, headers))
    response.append(dial)
    return str(response)


def build_message_ack_response(text: str) -> str:
    response = MessagingResponse()
    response.message(text)
    return str(response)


def _append_sip_headers(uri: str, headers: dict[str, str]) -> str:
    """Twilio accepts SIP custom headers as URI query parameters."""
    parsed = urlparse(uri)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(headers)
    return urlunparse(parsed._replace(query=urlencode(query)))


def extract_twilio_error(exc: Exception) -> tuple[str | None, str | None]:
    if isinstance(exc, TwilioRestException):
        return str(exc.code) if exc.code else None, exc.msg
    return None, str(exc)
