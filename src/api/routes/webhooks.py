"""Twilio voice/messages, SendGrid email and LiveKit room webhooks."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from livekit import api as livekit_api

from src.core.config import settings
from src.services.channels import (
    create_call_event,
    create_message_event,
    ensure_contact,
    ensure_conversation,
    find_channel_by_address,
    find_channel_by_kind_and_address,
    get_owned_kwami,
    try_normalize_phone_number,
    update_call_event_status,
    update_message_event_status,
)
from src.services.email_service import process_inbound_email
from src.services.sendgrid_service import verify_inbound_webhook
from src.services.sessions import record_room_event
from src.services.twilio_service import (
    build_message_ack_response,
    build_voice_bridge_response,
    validate_twilio_request,
)

router = APIRouter()
logger = logging.getLogger("kwami-api.webhooks")


async def _form_payload(request: Request) -> dict[str, str]:
    form = await request.form()
    return {str(key): str(value) for key, value in form.multi_items()}


@router.post("/twilio/voice")
async def twilio_voice_webhook(request: Request):
    """Handle an inbound Twilio voice call and bridge it to LiveKit SIP.

    Called by Twilio, not users: the request must carry a valid `X-Twilio-Signature` (401 if invalid, 503 if no auth token is configured).
    Records the call and returns TwiML: `<Reject/>` if the dialed number maps to no channel, a `<Say>` message if SIP inbound is not configured,
    otherwise the SIP bridge TwiML carrying kwami, channel and user headers."""
    payload = await _form_payload(request)
    await validate_twilio_request(request, payload)

    # try_* rather than maybe_*: Twilio sends whatever the network gave it, and
    # a raise here becomes a 5xx, which Twilio retries -- one malformed caller ID
    # would otherwise turn into a redelivery storm.
    raw_called = payload.get("To") or payload.get("Called")
    called = try_normalize_phone_number(raw_called, settings.twilio_phone_country)
    caller = try_normalize_phone_number(
        payload.get("From") or payload.get("Caller"),
        settings.twilio_phone_country,
    )

    if not called:
        logger.warning("Inbound voice call with no usable destination number")
        return Response("<Response><Reject/></Response>", media_type="application/xml")

    channel = await find_channel_by_address(called)
    if not channel:
        # Not the number: whose it is, is the caller's business, not the log's.
        logger.warning("Inbound voice call to a number no channel has")
        return Response("<Response><Reject/></Response>", media_type="application/xml")

    contact = await ensure_contact(
        user_id=channel["user_id"],
        kwami_id=channel["kwami_id"],
        phone_number=caller or "unknown",
        display_name=payload.get("CallerName"),
    )
    conversation = await ensure_conversation(
        user_id=channel["user_id"],
        kwami_id=channel["kwami_id"],
        channel_id=channel["id"],
        kind="call",
        contact_id=contact["id"],
        external_thread_id=payload.get("CallSid"),
        metadata={"source": "twilio_voice"},
    )
    await create_call_event(
        conversation_id=conversation["id"],
        channel_id=channel["id"],
        user_id=channel["user_id"],
        kwami_id=channel["kwami_id"],
        direction="inbound",
        provider_call_sid=payload.get("CallSid"),
        livekit_room_name=None,
        participant_identity=None,
        from_number=caller,
        to_number=called,
        status=payload.get("CallStatus") or "ringing",
        provider_payload=payload,
    )

    if not settings.livekit_sip_inbound_uri:
        return Response(
            "<Response><Say>Voice is not configured for this number yet.</Say></Response>",
            media_type="application/xml",
        )

    xml = build_voice_bridge_response(
        livekit_uri=settings.livekit_sip_inbound_uri,
        headers={
            "X-Kwami-Id": channel["kwami_id"],
            "X-Kwami-Channel-Id": channel["id"],
            "X-Kwami-User-Id": channel["user_id"],
            "X-Contact-Number": caller or "",
        },
    )
    return Response(xml, media_type="application/xml")


@router.post("/twilio/voice/status")
async def twilio_voice_status_webhook(request: Request):
    """Update a call event from a Twilio voice status callback.

    Requires a valid Twilio signature (401, or 503 if unconfigured). Applies status, duration and error fields for the `CallSid`. Returns `{"ok": true}`."""
    payload = await _form_payload(request)
    await validate_twilio_request(request, payload)
    if payload.get("CallSid"):
        await update_call_event_status(
            payload["CallSid"],
            status=payload.get("CallStatus") or "completed",
            duration_seconds=int(payload["CallDuration"]) if payload.get("CallDuration") else None,
            error_code=payload.get("ErrorCode"),
            error_message=payload.get("SipResponseCode") or payload.get("ErrorMessage"),
            provider_payload=payload,
        )
    return JSONResponse({"ok": True})


@router.post("/twilio/whatsapp")
async def twilio_whatsapp_webhook(request: Request):
    """Handle an inbound WhatsApp or SMS message from Twilio.

    Requires a valid Twilio signature (401, or 503 if unconfigured). Messages with a `whatsapp:` address are treated as WhatsApp, others as SMS.
    Stores the inbound message and replies with TwiML containing a short acknowledgement, or a "not configured" notice for an unknown sender."""
    payload = await _form_payload(request)
    await validate_twilio_request(request, payload)

    to_address = payload.get("To") or ""
    from_address = payload.get("From") or ""
    body = payload.get("Body") or ""
    is_whatsapp = to_address.startswith("whatsapp:") or from_address.startswith("whatsapp:")
    channel_kind = "whatsapp" if is_whatsapp else "sms"
    if is_whatsapp:
        channel = await find_channel_by_kind_and_address("whatsapp", to_address)
    else:
        to_e164 = (
            try_normalize_phone_number(to_address, settings.twilio_phone_country) or to_address
        )
        channel = await find_channel_by_kind_and_address(
            "sms", to_e164
        ) or await find_channel_by_address(to_e164)
    if not channel:
        logger.warning("Inbound %s message for unknown sender: %s", channel_kind, to_address)
        return Response(
            build_message_ack_response("This sender is not configured."),
            media_type="application/xml",
        )

    raw_contact_address = from_address.replace("whatsapp:", "")
    contact_number = try_normalize_phone_number(raw_contact_address, settings.twilio_phone_country)
    contact = await ensure_contact(
        user_id=channel["user_id"],
        kwami_id=channel["kwami_id"],
        phone_number=contact_number or raw_contact_address or from_address,
        display_name=payload.get("ProfileName"),
        whatsapp_address=from_address if is_whatsapp else None,
    )
    conversation = await ensure_conversation(
        user_id=channel["user_id"],
        kwami_id=channel["kwami_id"],
        channel_id=channel["id"],
        kind="whatsapp",
        contact_id=contact["id"],
        external_thread_id=from_address,
        metadata={
            "source": "twilio_whatsapp" if is_whatsapp else "twilio_sms",
            "channelKind": channel_kind,
        },
    )
    await create_message_event(
        conversation_id=conversation["id"],
        channel_id=channel["id"],
        contact_id=contact["id"],
        user_id=channel["user_id"],
        kwami_id=channel["kwami_id"],
        direction="inbound",
        provider_message_sid=payload.get("MessageSid"),
        provider_status=payload.get("SmsStatus") or "received",
        from_address=from_address,
        to_address=to_address,
        body=body,
        requires_followup=True,
        provider_payload=payload,
    )

    kwami = await get_owned_kwami(channel["user_id"], channel["kwami_id"])
    if is_whatsapp:
        ack_text = (
            f"{kwami.get('name') or 'Kwami'} received your message. "
            "WhatsApp is connected, and an automated reply pipeline can now be layered on top of this thread."
        )
    else:
        ack_text = (
            f"{kwami.get('name') or 'Kwami'} received your SMS. SMS is connected for this number."
        )
    return Response(build_message_ack_response(ack_text), media_type="application/xml")


@router.post("/twilio/whatsapp/status")
async def twilio_whatsapp_status_webhook(request: Request):
    """Update a message event from a Twilio message status callback.

    Requires a valid Twilio signature (401, or 503 if unconfigured). Applies the status and error fields for the `MessageSid`. Returns `{"ok": true}`."""
    payload = await _form_payload(request)
    await validate_twilio_request(request, payload)
    if payload.get("MessageSid"):
        await update_message_event_status(
            payload["MessageSid"],
            provider_status=payload.get("MessageStatus") or payload.get("SmsStatus") or "sent",
            error_code=payload.get("ErrorCode"),
            error_message=payload.get("ErrorMessage"),
            provider_payload=payload,
        )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# SendGrid Inbound Parse
# ---------------------------------------------------------------------------


def _parse_address_list(raw: str) -> list[str]:
    """Extract email addresses from a SendGrid-style address string."""
    if not raw:
        return []
    return [a.strip().strip("<>") for a in raw.split(",") if "@" in a]


@router.post("/email/inbound")
async def sendgrid_inbound_email(request: Request):
    """Receive an email via SendGrid Inbound Parse (multipart/form-data)."""
    form = await request.form()

    # Not optional: verify_inbound_webhook raises on a missing secret, a stale
    # timestamp or a bad signature, the same way validate_twilio_request does.
    verify_inbound_webhook(
        str(form.get("token", "")),
        str(form.get("timestamp", "")),
        str(form.get("signature", "")),
    )

    from_address = str(form.get("from", ""))
    to_raw = str(form.get("to", ""))
    cc_raw = str(form.get("cc", ""))
    subject = str(form.get("subject", ""))
    body_text = str(form.get("text", ""))
    body_html = str(form.get("html", ""))

    # SendGrid may include an envelope JSON with clean addresses
    envelope_raw = str(form.get("envelope", "{}"))
    try:
        envelope = json.loads(envelope_raw)
    except (json.JSONDecodeError, TypeError):
        envelope = {}

    to_addresses = envelope.get("to") or _parse_address_list(to_raw)
    if isinstance(to_addresses, str):
        to_addresses = [to_addresses]

    headers_raw = str(form.get("headers", ""))
    headers_dict: dict[str, str] = {}
    for line in headers_raw.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            headers_dict[key.strip()] = val.strip()

    sendgrid_message_id = headers_dict.get("Message-ID") or headers_dict.get("Message-Id")

    stored = await process_inbound_email(
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=_parse_address_list(cc_raw) or None,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        headers=headers_dict,
        sendgrid_message_id=sendgrid_message_id,
    )

    if not stored:
        logger.warning("Inbound email dropped — no matching account")
    return JSONResponse({"ok": True})


def _livekit_receiver() -> livekit_api.WebhookReceiver:
    return livekit_api.WebhookReceiver(
        livekit_api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
    )


@router.post("/livekit")
async def livekit_webhook(request: Request):
    """Room lifecycle from LiveKit Cloud: started, joined, left, finished.

    LiveKit signs every delivery with a JWT made from this project's API key and
    secret, carrying the SHA-256 of the body; anything that does not verify is
    refused. Configure the URL `/webhooks/livekit` in the LiveKit project's
    webhook settings.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    token = request.headers.get("Authorization", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        event = _livekit_receiver().receive(body, token)
    except Exception:
        logger.warning("Rejected a LiveKit webhook that did not verify")
        return JSONResponse({"detail": "Invalid webhook signature"}, status_code=401)

    at = (
        datetime.fromtimestamp(event.created_at, UTC) if event.created_at > 0 else datetime.now(UTC)
    )
    try:
        updated = await record_room_event(event.event, event.room.name, at=at)
    except Exception:
        # Every update is idempotent, so a retry from LiveKit is the right recovery.
        logger.exception("Could not record LiveKit event %s for %s", event.event, event.room.name)
        return JSONResponse({"detail": "Could not record the event"}, status_code=503)
    return JSONResponse({"ok": True, "updated": updated})
