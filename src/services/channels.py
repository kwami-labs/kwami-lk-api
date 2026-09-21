"""Persistence helpers for kwami communications channels and events."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import phonenumbers

from src.core.errors import InvalidPhoneNumberError
from src.services.credits import get_supabase_admin
from src.services.idempotency import insert_or_existing

logger = logging.getLogger("kwami-api.channels")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_phone_number(value: str, default_region: str = "US") -> str:
    """Normalize input to E.164 for consistent channel/contact matching.

    Raises ``InvalidPhoneNumberError`` (a DomainError -> HTTP 400) for anything
    unparseable or invalid.

    ``phonenumbers.NumberParseException`` is NOT a subclass of ``ValueError`` --
    its bases are ``UnicodeMixin`` and ``Exception``. Every caller wrapped this in
    ``except ValueError``, so garbage input escaped as a 500 rather than a 400.
    On the Twilio webhooks that mattered twice over: a malformed inbound number
    returned 5xx, which Twilio retries, turning one bad caller ID into a retry
    storm.
    """
    try:
        parsed = phonenumbers.parse(value, default_region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumberError(f"Could not parse phone number: {exc}") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError("Phone number is not valid")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def maybe_normalize_phone_number(value: str | None, default_region: str = "US") -> str | None:
    if not value:
        return None
    return normalize_phone_number(value, default_region)


def try_normalize_phone_number(value: str | None, default_region: str = "US") -> str | None:
    """Normalize if possible, otherwise return None.

    For inbound webhooks, where a provider sends whatever the network gave it.
    Rejecting the request is the wrong response: the provider reads any non-2xx
    as failure and redelivers, so a permanently malformed number would be retried
    forever. Store what we can and carry on.
    """
    if not value:
        return None
    try:
        return normalize_phone_number(value, default_region)
    except InvalidPhoneNumberError:
        logger.warning("Could not normalize an inbound phone number; using it verbatim")
        return None


def _single(result: Any) -> dict[str, Any] | None:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    return data


async def get_owned_kwami(user_id: str, kwami_id: str) -> dict[str, Any]:
    sb = get_supabase_admin()
    result = (
        await sb.table("user_kwamis")
        .select("id, user_id, name, config, created_at, updated_at")
        .eq("id", kwami_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    row = _single(result)
    if not row:
        raise ValueError("Kwami not found")
    return row


async def list_channels_for_kwami(user_id: str, kwami_id: str) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_channels")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .order("created_at", desc=False)
        .execute()
    )
    return list(getattr(result, "data", None) or [])


async def list_channels_sharing_twilio_incoming(
    user_id: str,
    kwami_id: str,
    twilio_incoming_sid: str,
) -> list[dict[str, Any]]:
    """Voice + WhatsApp rows for the same Twilio IncomingPhoneNumber SID."""
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_channels")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("provider_channel_sid", twilio_incoming_sid)
        .execute()
    )
    return list(getattr(result, "data", None) or [])


async def delete_kwami_channels(user_id: str, channel_ids: list[str]) -> None:
    if not channel_ids:
        return
    sb = get_supabase_admin()
    await (
        sb.table("kwami_channels").delete().eq("user_id", user_id).in_("id", channel_ids).execute()
    )


async def get_channel(user_id: str, channel_id: str) -> dict[str, Any]:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_channels")
        .select("*")
        .eq("user_id", user_id)
        .eq("id", channel_id)
        .limit(1)
        .execute()
    )
    row = _single(result)
    if not row:
        raise ValueError("Channel not found")
    return row


async def get_channel_by_kind(user_id: str, kwami_id: str, kind: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_channels")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("kind", kind)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    return _single(result)


async def upsert_channel(
    *,
    user_id: str,
    kwami_id: str,
    kind: str,
    phone_number: str,
    country_code: str,
    status: str,
    display_name: str | None = None,
    provider: str = "twilio",
    capabilities: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    provider_channel_sid: str | None = None,
    provider_subresource_sid: str | None = None,
    provider_sender: str | None = None,
    livekit_outbound_trunk_id: str | None = None,
) -> dict[str, Any]:
    sb = get_supabase_admin()
    existing = (
        await sb.table("kwami_channels")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("kind", kind)
        .limit(1)
        .execute()
    )
    row = _single(existing)
    payload = {
        "user_id": user_id,
        "kwami_id": kwami_id,
        "kind": kind,
        "provider": provider,
        "status": status,
        "phone_number": phone_number,
        "display_name": display_name,
        "country_code": country_code,
        "capabilities": capabilities or {},
        "metadata": metadata or {},
        "provider_channel_sid": provider_channel_sid,
        "provider_subresource_sid": provider_subresource_sid,
        "provider_sender": provider_sender,
        "livekit_outbound_trunk_id": livekit_outbound_trunk_id,
    }
    if row:
        result = (
            await sb.table("kwami_channels")
            .update(payload)
            .eq("id", row["id"])
            .eq("user_id", user_id)
            .execute()
        )
        updated = _single(result)
        return updated or {**row, **payload}

    result = await sb.table("kwami_channels").insert(payload).execute()
    created = _single(result)
    if not created:
        raise RuntimeError("Failed to create channel")
    return created


async def update_channel(
    channel_id: str,
    *,
    user_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_channels")
        .update(updates)
        .eq("id", channel_id)
        .eq("user_id", user_id)
        .execute()
    )
    row = _single(result)
    if not row:
        raise ValueError("Channel not found")
    return row


async def find_channel_by_address(address: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    for field in ("phone_number", "provider_sender"):
        result = await sb.table("kwami_channels").select("*").eq(field, address).limit(1).execute()
        row = _single(result)
        if row:
            return row
    return None


async def find_channel_by_kind_and_address(kind: str, address: str) -> dict[str, Any] | None:
    sb = get_supabase_admin()
    for field in ("provider_sender", "phone_number"):
        result = (
            await sb.table("kwami_channels")
            .select("*")
            .eq("kind", kind)
            .eq(field, address)
            .limit(1)
            .execute()
        )
        row = _single(result)
        if row:
            return row
    return None


async def ensure_contact(
    *,
    user_id: str,
    kwami_id: str,
    phone_number: str,
    display_name: str | None = None,
    whatsapp_address: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_contacts")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("phone_number", phone_number)
        .limit(1)
        .execute()
    )
    row = _single(result)
    payload = {
        "user_id": user_id,
        "kwami_id": kwami_id,
        "phone_number": phone_number,
        "display_name": display_name,
        "whatsapp_address": whatsapp_address,
        "metadata": metadata or {},
    }
    if row:
        merged_metadata = {**(row.get("metadata") or {}), **payload["metadata"]}
        update = {
            "display_name": display_name or row.get("display_name"),
            "whatsapp_address": whatsapp_address or row.get("whatsapp_address"),
            "metadata": merged_metadata,
        }
        updated = await sb.table("kwami_contacts").update(update).eq("id", row["id"]).execute()
        return _single(updated) or {**row, **update}

    created = await sb.table("kwami_contacts").insert(payload).execute()
    row = _single(created)
    if not row:
        raise RuntimeError("Failed to create contact")
    return row


async def list_contacts_for_kwami(
    user_id: str,
    kwami_id: str,
    *,
    query: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    select_query = (
        sb.table("kwami_contacts").select("*").eq("user_id", user_id).eq("kwami_id", kwami_id)
    )
    if query and query.strip():
        like = f"%{query.strip()}%"
        select_query = select_query.or_(
            f"display_name.ilike.{like},phone_number.ilike.{like},email.ilike.{like},instagram.ilike.{like},tiktok.ilike.{like}"
        )
    result = await select_query.order("updated_at", desc=True).limit(limit).execute()
    return list(getattr(result, "data", None) or [])


async def get_contact(user_id: str, contact_id: str) -> dict[str, Any]:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_contacts")
        .select("*")
        .eq("id", contact_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    row = _single(result)
    if not row:
        raise ValueError("Contact not found")
    return row


async def create_contact(
    *,
    user_id: str,
    kwami_id: str,
    display_name: str,
    phone_number: str,
    whatsapp_address: str | None = None,
    email: str | None = None,
    instagram: str | None = None,
    tiktok: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sb = get_supabase_admin()
    payload = {
        "user_id": user_id,
        "kwami_id": kwami_id,
        "display_name": display_name,
        "phone_number": phone_number,
        "whatsapp_address": whatsapp_address,
        "email": email,
        "instagram": instagram,
        "tiktok": tiktok,
        "metadata": metadata or {},
    }
    created = await sb.table("kwami_contacts").insert(payload).execute()
    row = _single(created)
    if not row:
        raise RuntimeError("Failed to create contact")
    return row


async def update_contact(
    *,
    user_id: str,
    contact_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_contacts")
        .update(updates)
        .eq("id", contact_id)
        .eq("user_id", user_id)
        .execute()
    )
    row = _single(result)
    if not row:
        raise ValueError("Contact not found")
    return row


async def delete_contact(user_id: str, contact_id: str) -> None:
    sb = get_supabase_admin()
    await sb.table("kwami_contacts").delete().eq("id", contact_id).eq("user_id", user_id).execute()


async def ensure_conversation(
    *,
    user_id: str,
    kwami_id: str,
    channel_id: str,
    kind: str,
    contact_id: str | None = None,
    external_thread_id: str | None = None,
    status: str = "active",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sb = get_supabase_admin()
    query = (
        sb.table("kwami_conversations")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("channel_id", channel_id)
        .eq("kind", kind)
    )
    if contact_id:
        query = query.eq("contact_id", contact_id)
    result = await query.order("updated_at", desc=True).limit(1).execute()
    row = _single(result)
    payload = {
        "user_id": user_id,
        "kwami_id": kwami_id,
        "channel_id": channel_id,
        "contact_id": contact_id,
        "kind": kind,
        "status": status,
        "external_thread_id": external_thread_id,
        "metadata": metadata or {},
    }
    if row:
        merged_metadata = {**(row.get("metadata") or {}), **payload["metadata"]}
        update = {
            "status": status,
            "external_thread_id": external_thread_id or row.get("external_thread_id"),
            "metadata": merged_metadata,
        }
        updated = await sb.table("kwami_conversations").update(update).eq("id", row["id"]).execute()
        return _single(updated) or {**row, **update}

    created = await sb.table("kwami_conversations").insert(payload).execute()
    row = _single(created)
    if not row:
        raise RuntimeError("Failed to create conversation")
    return row


async def touch_conversation(
    conversation_id: str,
    *,
    direction: str,
) -> None:
    sb = get_supabase_admin()
    field = "last_inbound_at" if direction == "inbound" else "last_outbound_at"
    await (
        sb.table("kwami_conversations")
        .update({field: now_iso()})
        .eq("id", conversation_id)
        .execute()
    )


async def create_call_event(
    *,
    conversation_id: str | None,
    channel_id: str,
    user_id: str,
    kwami_id: str,
    direction: str,
    provider_call_sid: str | None,
    livekit_room_name: str | None,
    participant_identity: str | None,
    from_number: str | None,
    to_number: str | None,
    status: str,
    duration_seconds: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    provider_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "conversation_id": conversation_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "kwami_id": kwami_id,
        "direction": direction,
        "provider_call_sid": provider_call_sid,
        "livekit_room_name": livekit_room_name,
        "participant_identity": participant_identity,
        "from_number": from_number,
        "to_number": to_number,
        "status": status,
        "duration_seconds": duration_seconds,
        "error_code": error_code,
        "error_message": error_message,
        "provider_payload": provider_payload or {},
    }
    # Same for a redelivered CallSid.
    row = await insert_or_existing(
        "kwami_call_events", payload, conflict_column="provider_call_sid"
    )
    if not row:
        raise RuntimeError("Failed to create call event")
    if conversation_id:
        await touch_conversation(conversation_id, direction=direction)
    return row


async def update_call_event_status(
    provider_call_sid: str,
    *,
    status: str,
    duration_seconds: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    provider_payload: dict[str, Any] | None = None,
) -> None:
    sb = get_supabase_admin()
    updates: dict[str, Any] = {"status": status}
    if duration_seconds is not None:
        updates["duration_seconds"] = duration_seconds
    if error_code is not None:
        updates["error_code"] = error_code
    if error_message is not None:
        updates["error_message"] = error_message
    if provider_payload is not None:
        updates["provider_payload"] = provider_payload
    await (
        sb.table("kwami_call_events")
        .update(updates)
        .eq("provider_call_sid", provider_call_sid)
        .execute()
    )


async def create_message_event(
    *,
    conversation_id: str | None,
    channel_id: str,
    contact_id: str | None,
    user_id: str,
    kwami_id: str,
    direction: str,
    provider_message_sid: str | None,
    provider_status: str | None,
    from_address: str | None,
    to_address: str | None,
    body: str | None,
    error_code: str | None = None,
    error_message: str | None = None,
    requires_followup: bool = False,
    provider_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "conversation_id": conversation_id,
        "channel_id": channel_id,
        "contact_id": contact_id,
        "user_id": user_id,
        "kwami_id": kwami_id,
        "direction": direction,
        "provider_message_sid": provider_message_sid,
        "provider_status": provider_status,
        "from_address": from_address,
        "to_address": to_address,
        "body": body,
        "error_code": error_code,
        "error_message": error_message,
        "requires_followup": requires_followup,
        "provider_payload": provider_payload or {},
    }
    # A Twilio redelivery carries the same MessageSid, which the partial unique
    # index refuses. Returning the stored row keeps the retry a 2xx no-op.
    row = await insert_or_existing(
        "kwami_message_events", payload, conflict_column="provider_message_sid"
    )
    if not row:
        raise RuntimeError("Failed to create message event")
    if conversation_id:
        await touch_conversation(conversation_id, direction=direction)
    return row


async def update_message_event_status(
    provider_message_sid: str,
    *,
    provider_status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    provider_payload: dict[str, Any] | None = None,
) -> None:
    sb = get_supabase_admin()
    updates: dict[str, Any] = {"provider_status": provider_status}
    if error_code is not None:
        updates["error_code"] = error_code
    if error_message is not None:
        updates["error_message"] = error_message
    if provider_payload is not None:
        updates["provider_payload"] = provider_payload
    await (
        sb.table("kwami_message_events")
        .update(updates)
        .eq("provider_message_sid", provider_message_sid)
        .execute()
    )


async def recent_events_for_kwami(user_id: str, kwami_id: str) -> dict[str, list[dict[str, Any]]]:
    sb = get_supabase_admin()
    calls = (
        await sb.table("kwami_call_events")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .order("created_at", desc=True)
        .limit(25)
        .execute()
    )
    messages = (
        await sb.table("kwami_message_events")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .order("created_at", desc=True)
        .limit(25)
        .execute()
    )
    return {
        "calls": list(getattr(calls, "data", None) or []),
        "messages": list(getattr(messages, "data", None) or []),
    }


def build_agent_bootstrap_payload(kwami_row: dict[str, Any]) -> dict[str, Any]:
    """Map saved frontend workspace config to the agent 'config' message format."""
    config = kwami_row.get("config") or {}
    voice_state = config.get("voice") or {}
    soul_state = voice_state.get("soulConfig") or {}

    return {
        "type": "config",
        "kwamiId": kwami_row["id"],
        "kwamiName": kwami_row.get("name") or "Kwami",
        "voice": {
            "stt": {
                "provider": ((voice_state.get("stt") or {}).get("provider") or "deepgram"),
                "model": ((voice_state.get("stt") or {}).get("model") or "nova-2-phonecall"),
                "language": ((voice_state.get("stt") or {}).get("language") or "en"),
            },
            "llm": {
                "provider": ((voice_state.get("llm") or {}).get("provider") or "openai"),
                "model": ((voice_state.get("llm") or {}).get("model") or "gpt-4o-mini"),
                "temperature": ((voice_state.get("llm") or {}).get("temperature") or 0.7),
                "maxTokens": ((voice_state.get("llm") or {}).get("maxTokens") or 1024),
            },
            "tts": {
                "provider": ((voice_state.get("tts") or {}).get("provider") or "openai"),
                "model": ((voice_state.get("tts") or {}).get("model") or "tts-1"),
                "voice": ((voice_state.get("tts") or {}).get("voice") or "nova"),
                "speed": ((voice_state.get("tts") or {}).get("speed") or 1.0),
            },
        },
        "soul": {
            "name": soul_state.get("name") or kwami_row.get("name") or "Kwami",
            "personality": soul_state.get("personality") or "",
            "systemPrompt": soul_state.get("systemPrompt") or "",
            "traits": soul_state.get("traits") or [],
            "conversationStyle": soul_state.get("conversationStyle") or "friendly",
            "responseLength": soul_state.get("responseLength") or "medium",
            "emotionalTone": soul_state.get("emotionalTone") or "warm",
        },
        "memory": {"enabled": True},
    }
