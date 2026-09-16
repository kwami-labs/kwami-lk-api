"""LiveKit SIP helpers for outbound calling."""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import HTTPException
from livekit import api as livekit_api
from livekit.api.sip_service import (
    CreateSIPParticipantRequest,
    ListSIPInboundTrunkRequest,
    ListSIPOutboundTrunkRequest,
)
from livekit.api.twirp_client import TwirpError
from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

from src.core.config import settings

logger = logging.getLogger("kwami-api.telephony")


def _twirp_sip_client_detail(exc: TwirpError) -> str:
    """Append LiveKit metadata and a short hint for SIP failures (SCL_… ids, PCAP in Cloud UI)."""
    meta = exc.metadata or {}
    bits: list[str] = []
    for key in sorted(meta.keys()):
        bits.append(f"{key}={meta[key]}")
    meta_str = (" (" + "; ".join(bits) + ")") if bits else ""
    hint = (
        " In LiveKit Cloud open Telephony → Calls, locate this SIP session (id often starts with SCL_), "
        "and download the PCAP to inspect the SIP response (e.g. 401/403/404/603). "
        "Twilio-only calls use the REST API; this path uses your LiveKit outbound trunk → Twilio SIP termination."
    )
    return meta_str + hint


def build_call_room_name(kwami_id: str) -> str:
    return f"kwami-call-{kwami_id[:8]}-{uuid4().hex[:10]}"


def _response_items(response: object) -> list[object]:
    for field_name in ("items", "trunks"):
        value = getattr(response, field_name, None)
        if value:
            return list(value)
    return []


async def _find_outbound_trunk(lkapi: livekit_api.LiveKitAPI, trunk_id: str) -> object | None:
    response = await lkapi.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
    for trunk in _response_items(response):
        if getattr(trunk, "sip_trunk_id", None) == trunk_id:
            return trunk
    return None


async def _find_inbound_trunk(lkapi: livekit_api.LiveKitAPI, trunk_id: str) -> object | None:
    response = await lkapi.sip.list_inbound_trunk(ListSIPInboundTrunkRequest())
    for trunk in _response_items(response):
        if getattr(trunk, "sip_trunk_id", None) == trunk_id:
            return trunk
    return None


def _merged_numbers(existing: object, phone_number: str) -> list[str]:
    current = list(getattr(existing, "numbers", None) or [])
    merged = list(dict.fromkeys([*current, phone_number]))
    return merged


async def sync_shared_livekit_trunks(phone_number: str) -> dict[str, object]:
    """Ensure a purchased number is usable on shared LiveKit SIP infrastructure."""
    sync: dict[str, object] = {
        "outbound": {"configured": bool(settings.livekit_sip_outbound_trunk_id), "synced": False},
        "inbound": {"configured": bool(settings.livekit_sip_inbound_trunk_id), "synced": False},
        "strategy": "shared_trunks",
        "notes": [],
    }

    async with livekit_api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as lkapi:
        if settings.livekit_sip_outbound_trunk_id:
            outbound = await _find_outbound_trunk(lkapi, settings.livekit_sip_outbound_trunk_id)
            if not outbound:
                raise HTTPException(
                    status_code=503, detail="Configured LiveKit outbound SIP trunk was not found"
                )
            outbound_numbers = _merged_numbers(outbound, phone_number)
            await lkapi.sip.update_outbound_trunk_fields(
                settings.livekit_sip_outbound_trunk_id,
                numbers=outbound_numbers,
            )
            sync["outbound"] = {
                "configured": True,
                "synced": True,
                "numberCount": len(outbound_numbers),
            }
        else:
            sync["notes"].append(
                "No shared LiveKit outbound trunk is configured yet, so this number cannot be used as caller ID for outbound calls."
            )

        if settings.livekit_sip_inbound_trunk_id:
            inbound = await _find_inbound_trunk(lkapi, settings.livekit_sip_inbound_trunk_id)
            if not inbound:
                raise HTTPException(
                    status_code=503, detail="Configured LiveKit inbound SIP trunk was not found"
                )
            inbound_numbers = _merged_numbers(inbound, phone_number)
            await lkapi.sip.update_inbound_trunk_fields(
                settings.livekit_sip_inbound_trunk_id,
                numbers=inbound_numbers,
            )
            sync["inbound"] = {
                "configured": True,
                "synced": True,
                "numberCount": len(inbound_numbers),
            }
        else:
            sync["inbound"] = {
                "configured": False,
                "synced": True,
                "strategy": "shared_voice_webhook",
            }
            sync["notes"].append(
                "Inbound routing uses the shared Twilio voice webhook, so the LiveKit inbound trunk does not need an explicit per-number allowlist."
            )

    return sync


def _numbers_without(existing: object, phone_number: str) -> list[str]:
    current = list(getattr(existing, "numbers", None) or [])
    return [n for n in current if n != phone_number]


async def remove_phone_from_shared_livekit_trunks(phone_number: str) -> dict[str, object]:
    """Remove an E.164 number from shared outbound/inbound LiveKit SIP trunks (reverse of sync)."""
    sync: dict[str, object] = {
        "outbound": {"configured": bool(settings.livekit_sip_outbound_trunk_id), "synced": False},
        "inbound": {"configured": bool(settings.livekit_sip_inbound_trunk_id), "synced": False},
        "strategy": "shared_trunks",
    }

    async with livekit_api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as lkapi:
        if settings.livekit_sip_outbound_trunk_id:
            outbound = await _find_outbound_trunk(lkapi, settings.livekit_sip_outbound_trunk_id)
            if outbound:
                remaining = _numbers_without(outbound, phone_number)
                await lkapi.sip.update_outbound_trunk_fields(
                    settings.livekit_sip_outbound_trunk_id,
                    numbers=remaining,
                )
                sync["outbound"] = {
                    "configured": True,
                    "synced": True,
                    "numberCount": len(remaining),
                }
            else:
                logger.warning(
                    "LiveKit outbound trunk %s not found; skip number removal",
                    settings.livekit_sip_outbound_trunk_id,
                )

        if settings.livekit_sip_inbound_trunk_id:
            inbound = await _find_inbound_trunk(lkapi, settings.livekit_sip_inbound_trunk_id)
            if inbound:
                remaining_in = _numbers_without(inbound, phone_number)
                await lkapi.sip.update_inbound_trunk_fields(
                    settings.livekit_sip_inbound_trunk_id,
                    numbers=remaining_in,
                )
                sync["inbound"] = {
                    "configured": True,
                    "synced": True,
                    "numberCount": len(remaining_in),
                }
            else:
                logger.warning(
                    "LiveKit inbound trunk %s not found; skip number removal",
                    settings.livekit_sip_inbound_trunk_id,
                )

    return sync


async def create_outbound_call(
    *,
    kwami_id: str,
    phone_number: str,
    caller_id: str,
    participant_name: str,
    wait_until_answered: bool = False,
) -> dict[str, str]:
    if not settings.livekit_sip_outbound_trunk_id:
        raise HTTPException(
            status_code=503,
            detail=(
                "LiveKit SIP outbound trunk is not configured. "
                "Set LIVEKIT_SIP_OUTBOUND_TRUNK_ID (and LIVEKIT_URL / API credentials) on the API server."
            ),
        )

    room_name = build_call_room_name(kwami_id)
    participant_identity = f"sip_{uuid4().hex[:12]}"
    participant_metadata = f'{{"kwami_id":"{kwami_id}","channel":"voice_phone"}}'
    participant_attributes = {
        settings.livekit_sip_participant_attribute_key: kwami_id,
        "channel": "voice_phone",
    }

    request = CreateSIPParticipantRequest(
        sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
        sip_call_to=phone_number,
        sip_number=caller_id,
        room_name=room_name,
        participant_identity=participant_identity,
        participant_name=participant_name,
        participant_metadata=participant_metadata,
        participant_attributes=participant_attributes,
        wait_until_answered=wait_until_answered,
        play_dialtone=True,
    )

    async with livekit_api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as lkapi:
        try:
            participant = await lkapi.sip.create_sip_participant(request)
        except TwirpError as exc:
            logger.warning(
                "LiveKit CreateSIPParticipant failed: code=%s msg=%s http=%s metadata=%s",
                exc.code,
                exc.message,
                exc.status,
                exc.metadata,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "LiveKit could not start the outbound SIP call. "
                    f"{exc.code}: {exc.message or 'unknown error'}." + _twirp_sip_client_detail(exc)
                ),
            ) from exc

        dispatch_metadata = json.dumps({"kwami_id": kwami_id, "channel": "voice_phone"})
        try:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                CreateAgentDispatchRequest(
                    agent_name=settings.livekit_agent_name,
                    room=room_name,
                    metadata=dispatch_metadata,
                )
            )
        except TwirpError as exc:
            logger.warning(
                "LiveKit CreateDispatch failed: code=%s msg=%s http=%s metadata=%s",
                exc.code,
                exc.message,
                exc.status,
                exc.metadata,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Outbound SIP leg was created but the Kwami agent could not be dispatched to the call room. "
                    f"{exc.code}: {exc.message or 'unknown error'}."
                    + (
                        f" ({'; '.join(f'{k}={v}' for k, v in sorted((exc.metadata or {}).items()))})"
                        if exc.metadata
                        else ""
                    )
                    + " Ensure LIVEKIT_AGENT_NAME matches your worker and the agent is deployed."
                ),
            ) from exc

    logger.info(
        "Created outbound SIP participant and agent dispatch for kwami=%s room=%s to=%s dispatch_id=%s",
        kwami_id,
        room_name,
        phone_number,
        getattr(dispatch, "id", None),
    )
    return {
        "room_name": room_name,
        "participant_identity": participant.participant_identity,
        "provider_call_sid": participant.sip_call_id,
        "agent_dispatch_id": getattr(dispatch, "id", None) or "",
    }
