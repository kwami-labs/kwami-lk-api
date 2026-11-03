"""Reminders, things being followed up, and the settings of the scheduled reports.

The voice session ends; these do not. Times are stored twice on purpose: the
rule the user gave (08:00 on Mondays, in their zone) and the next instant it
falls on, because "every Monday at eight" has to stay at eight across a clock
change, and the scheduler can only look up an instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.services.channels import get_owned_kwami, normalize_phone_number, now_iso
from src.services.credits import get_supabase_admin

DEFAULT_TIMEZONE = "Europe/Madrid"
USER_KINDS = {"reminder", "followup", "work"}
REPEATING = {"daily", "weekly"}
STATUSES = {"open", "done", "cancelled"}
NOTIFY_CHANNELS = {"whatsapp", "sms"}
MAX_ROWS = 200

#: The user's rule (2026-11-01): reports on Mondays and Fridays, and on Sundays a
#: reminder of what is coming the week after. weekday, local time, title.
REPORTS: dict[str, tuple[int, str, str]] = {
    "monday": (0, "08:00", "Informe del lunes"),
    "friday": (4, "18:00", "Informe del viernes"),
    "sunday": (6, "20:00", "Recordatorio del domingo"),
}


def _single(result: Any) -> dict[str, Any] | None:
    data = getattr(result, "data", None)
    return data[0] if data else None


def parse_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or DEFAULT_TIMEZONE).strip())
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError(f"Unknown timezone '{name}'") from exc


def parse_local_time(value: str | None) -> time:
    try:
        return time.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValueError("local_time must look like 08:30") from exc


def parse_instant(value: str, zone: ZoneInfo) -> datetime:
    """An ISO instant; one with no offset is read in the user's zone, not in UTC."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid run_at") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)


def next_occurrence(
    recurrence: str,
    weekdays: list[int],
    local_time: str | None,
    timezone: str | None,
    after: datetime,
) -> datetime | None:
    """The first instant of the rule strictly after `after`, or None if it has no more."""
    if recurrence not in REPEATING:
        return None
    zone = parse_zone(timezone)
    at = parse_local_time(local_time)
    allowed = set(range(7)) if recurrence == "daily" else set(weekdays)
    if not allowed or not allowed <= set(range(7)):
        raise ValueError("Weekdays must be numbers from 0 (Monday) to 6 (Sunday)")
    day = after.astimezone(zone).date()
    # Eight days: the same weekday next week, when today's time has already gone.
    candidates = (
        datetime.combine(day + timedelta(days=offset), at, tzinfo=zone) for offset in range(8)
    )
    return next(c for c in candidates if c.weekday() in allowed and c > after).astimezone(UTC)


async def get_settings(user_id: str, kwami_id: str) -> dict[str, Any]:
    await get_owned_kwami(user_id, kwami_id)
    result = await (
        get_supabase_admin()
        .table("kwami_assistant_settings")
        .select("*")
        .eq("kwami_id", kwami_id)
        .limit(1)
        .execute()
    )
    return _single(result) or {
        "kwami_id": kwami_id,
        "user_id": user_id,
        "notify_to": None,
        "notify_channel": "whatsapp",
        "timezone": DEFAULT_TIMEZONE,
        "reports_enabled": False,
    }


async def save_settings(
    user_id: str,
    kwami_id: str,
    *,
    notify_to: str | None,
    notify_channel: str,
    timezone: str,
    reports_enabled: bool,
    default_region: str = "ES",
) -> dict[str, Any]:
    await get_owned_kwami(user_id, kwami_id)
    if notify_channel not in NOTIFY_CHANNELS:
        raise ValueError("notify_channel must be 'whatsapp' or 'sms'")
    parse_zone(timezone)
    number = normalize_phone_number(notify_to, default_region) if notify_to else None
    result = await (
        get_supabase_admin()
        .table("kwami_assistant_settings")
        .upsert(
            {
                "kwami_id": kwami_id,
                "user_id": user_id,
                "notify_to": number,
                "notify_channel": notify_channel,
                "timezone": timezone,
                "reports_enabled": reports_enabled,
            },
            on_conflict="kwami_id",
        )
        .execute()
    )
    await _sync_reports(user_id, kwami_id, timezone, reports_enabled)
    row = _single(result)
    if not row:
        raise RuntimeError("Failed to save the assistant settings")
    return row


async def _sync_reports(user_id: str, kwami_id: str, timezone: str, enabled: bool) -> None:
    """One row per report, switched rather than recreated, so its history stays."""
    sb = get_supabase_admin()
    now = datetime.now(UTC)
    for report_type, (weekday, local_time, title) in REPORTS.items():
        next_run = next_occurrence("weekly", [weekday], local_time, timezone, now)
        await (
            sb.table("kwami_assistant_tasks")
            .upsert(
                {
                    "user_id": user_id,
                    "kwami_id": kwami_id,
                    "kind": "report",
                    "report_type": report_type,
                    "title": title,
                    "recurrence": "weekly",
                    "weekdays": [weekday],
                    "local_time": local_time,
                    "timezone": timezone,
                    "next_run_at": next_run.isoformat() if enabled and next_run else None,
                    "status": "open" if enabled else "cancelled",
                },
                on_conflict="kwami_id,report_type",
            )
            .execute()
        )


async def list_tasks(
    user_id: str, kwami_id: str, *, status: str | None = None
) -> list[dict[str, Any]]:
    await get_owned_kwami(user_id, kwami_id)
    if status is not None and status not in STATUSES:
        raise ValueError(f"Invalid status '{status}'")
    query = (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
    )
    if status is not None:
        query = query.eq("status", status)
    result = await query.order("created_at", desc=True).limit(MAX_ROWS).execute()
    return list(getattr(result, "data", None) or [])


def _schedule_fields(
    *,
    run_at: str | None,
    recurrence: str | None,
    weekdays: list[int] | None,
    local_time: str | None,
    timezone: str,
    now: datetime,
) -> dict[str, Any]:
    zone = parse_zone(timezone)
    if run_at:
        instant = parse_instant(run_at, zone)
        if instant <= now:
            raise ValueError("run_at is in the past")
        return {
            "recurrence": "once",
            "weekdays": [],
            "local_time": None,
            "timezone": timezone,
            "next_run_at": instant.astimezone(UTC).isoformat(),
        }
    rule = recurrence or "none"
    if rule != "none" and rule not in REPEATING:
        raise ValueError("A one-off task takes run_at; a repeating one, daily or weekly")
    next_run = next_occurrence(rule, weekdays or [], local_time, timezone, now)
    return {
        "recurrence": rule,
        "weekdays": weekdays or [],
        "local_time": local_time,
        "timezone": timezone,
        "next_run_at": next_run.isoformat() if next_run else None,
    }


async def create_task(
    user_id: str,
    kwami_id: str,
    *,
    kind: str,
    title: str,
    notes: str = "",
    run_at: str | None = None,
    recurrence: str | None = None,
    weekdays: list[int] | None = None,
    local_time: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    settings_row = await get_settings(user_id, kwami_id)
    if kind not in USER_KINDS:
        raise ValueError("kind must be 'reminder', 'followup' or 'work'")
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Title is required")
    schedule = _schedule_fields(
        run_at=run_at,
        recurrence=recurrence,
        weekdays=weekdays,
        local_time=local_time,
        timezone=timezone or settings_row["timezone"],
        now=datetime.now(UTC),
    )
    if kind == "reminder" and not schedule["next_run_at"]:
        raise ValueError("A reminder needs a time")
    if kind == "work" and not schedule["next_run_at"]:
        raise ValueError("Scheduled work needs a time")
    if kind == "work" and not notes.strip():
        raise ValueError("Scheduled work needs the instruction in notes")
    created = await (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .insert(
            {
                "user_id": user_id,
                "kwami_id": kwami_id,
                "kind": kind,
                "title": clean_title,
                "notes": notes.strip(),
                "status": "open",
                **schedule,
            }
        )
        .execute()
    )
    row = _single(created)
    if not row:
        raise RuntimeError("Failed to create the task")
    return row


async def update_task(
    user_id: str,
    task_id: str,
    *,
    title: str | None = None,
    notes: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"Invalid status '{status}'")
    payload: dict[str, Any] = {"updated_at": now_iso()}
    if title is not None:
        if not title.strip():
            raise ValueError("Title is required")
        payload["title"] = title.strip()
    if notes is not None:
        payload["notes"] = notes.strip()
    if status is not None:
        payload["status"] = status
        if status != "open":
            payload["next_run_at"] = None
    result = await (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .update(payload)
        .eq("id", task_id)
        .eq("user_id", user_id)
        .neq("kind", "report")
        .execute()
    )
    row = _single(result)
    if not row:
        raise ValueError("Task not found")
    return row


async def delete_task(user_id: str, task_id: str) -> bool:
    result = await (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .delete()
        .eq("id", task_id)
        .eq("user_id", user_id)
        .neq("kind", "report")
        .execute()
    )
    return bool(getattr(result, "data", None))


async def list_notifications(
    user_id: str, kwami_id: str, *, unread_only: bool = False, limit: int = 20
) -> list[dict[str, Any]]:
    await get_owned_kwami(user_id, kwami_id)
    query = (
        get_supabase_admin()
        .table("kwami_assistant_notifications")
        .select("*")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
    )
    if unread_only:
        query = query.is_("read_at", "null")
    result = await query.order("created_at", desc=True).limit(min(limit, MAX_ROWS)).execute()
    return list(getattr(result, "data", None) or [])


async def mark_notification_read(user_id: str, notification_id: str) -> bool:
    result = await (
        get_supabase_admin()
        .table("kwami_assistant_notifications")
        .update({"read_at": now_iso()})
        .eq("id", notification_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(getattr(result, "data", None))


async def record_express_send(
    user_id: str,
    kwami_id: str,
    *,
    channel: str,
    recipient: str,
    summary: str,
    express_order: str,
) -> dict[str, Any]:
    """Every send that skipped the dialog, with the user's words that allowed it."""
    await get_owned_kwami(user_id, kwami_id)
    result = await (
        get_supabase_admin()
        .table("kwami_outbound_audit")
        .insert(
            {
                "user_id": user_id,
                "kwami_id": kwami_id,
                "channel": channel,
                "recipient": recipient,
                "summary": summary[:300],
                "express_order": express_order[:500],
            }
        )
        .execute()
    )
    row = _single(result)
    if not row:
        raise RuntimeError("Failed to record the send")
    return row
