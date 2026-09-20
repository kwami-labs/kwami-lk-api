"""Persistence and validation helpers for kwami calendar events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.services.channels import get_owned_kwami
from src.services.credits import get_supabase_admin

VALID_EVENT_TYPES = {"meeting", "task", "personal", "reminder", "focus", "other"}


def _single(result: Any) -> dict[str, Any] | None:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    return data


def _parse_iso(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _normalize_event_type(value: str | None) -> str:
    candidate = (value or "other").strip().lower()
    if candidate not in VALID_EVENT_TYPES:
        raise ValueError(f"Invalid event_type '{candidate}'")
    return candidate


def _normalize_color(value: str | None) -> str:
    color = (value or "#6366f1").strip()
    if len(color) > 32:
        raise ValueError("Color value is too long")
    return color


async def _ensure_kwami_owned(user_id: str, kwami_id: str) -> None:
    await get_owned_kwami(user_id, kwami_id)


async def list_events(
    user_id: str,
    kwami_id: str,
    range_start: str,
    range_end: str,
) -> list[dict[str, Any]]:
    await _ensure_kwami_owned(user_id, kwami_id)
    start = _parse_iso(range_start, "range_start")
    end = _parse_iso(range_end, "range_end")
    if end < start:
        raise ValueError("range_end must be after range_start")

    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_calendar_events")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .lte("starts_at", end.isoformat())
        .gte("ends_at", start.isoformat())
        .order("starts_at", desc=False)
        .execute()
    )
    return list(getattr(result, "data", None) or [])


async def create_event(
    user_id: str,
    kwami_id: str,
    *,
    title: str,
    starts_at: str,
    ends_at: str,
    description: str = "",
    all_day: bool = False,
    event_type: str | None = None,
    color: str | None = None,
    location: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    await _ensure_kwami_owned(user_id, kwami_id)
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Title is required")

    start = _parse_iso(starts_at, "starts_at")
    end = _parse_iso(ends_at, "ends_at")
    if end < start:
        raise ValueError("ends_at must be after starts_at")

    payload = {
        "user_id": user_id,
        "kwami_id": kwami_id,
        "title": clean_title,
        "description": description.strip(),
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "all_day": bool(all_day),
        "event_type": _normalize_event_type(event_type),
        "color": _normalize_color(color),
        "location": location.strip(),
        "metadata": metadata or {},
    }
    sb = get_supabase_admin()
    created = await sb.table("kwami_calendar_events").insert(payload).execute()
    row = _single(created)
    if not row:
        raise RuntimeError("Failed to create calendar event")
    return row


async def update_event(
    user_id: str,
    event_id: str,
    *,
    title: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    description: str | None = None,
    all_day: bool | None = None,
    event_type: str | None = None,
    color: str | None = None,
    location: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sb = get_supabase_admin()
    existing_result = (
        await sb.table("kwami_calendar_events")
        .select("*")
        .eq("id", event_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    existing = _single(existing_result)
    if not existing:
        raise ValueError("Event not found")
    await _ensure_kwami_owned(user_id, str(existing["kwami_id"]))

    next_start = (
        _parse_iso(starts_at, "starts_at")
        if starts_at
        else _parse_iso(str(existing["starts_at"]), "starts_at")
    )
    next_end = (
        _parse_iso(ends_at, "ends_at")
        if ends_at
        else _parse_iso(str(existing["ends_at"]), "ends_at")
    )
    if next_end < next_start:
        raise ValueError("ends_at must be after starts_at")

    updates: dict[str, Any] = {
        "starts_at": next_start.isoformat(),
        "ends_at": next_end.isoformat(),
    }
    if title is not None:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Title is required")
        updates["title"] = clean_title
    if description is not None:
        updates["description"] = description.strip()
    if all_day is not None:
        updates["all_day"] = bool(all_day)
    if event_type is not None:
        updates["event_type"] = _normalize_event_type(event_type)
    if color is not None:
        updates["color"] = _normalize_color(color)
    if location is not None:
        updates["location"] = location.strip()
    if metadata is not None:
        updates["metadata"] = metadata

    result = (
        await sb.table("kwami_calendar_events")
        .update(updates)
        .eq("id", event_id)
        .eq("user_id", user_id)
        .execute()
    )
    row = _single(result)
    if not row:
        raise ValueError("Event not found")
    return row


async def delete_event(user_id: str, event_id: str) -> bool:
    sb = get_supabase_admin()
    result = (
        await sb.table("kwami_calendar_events")
        .delete()
        .eq("id", event_id)
        .eq("user_id", user_id)
        .execute()
    )
    deleted = getattr(result, "data", None) or []
    return len(deleted) > 0
