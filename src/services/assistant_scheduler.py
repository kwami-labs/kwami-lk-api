"""The clock: fires what is due, and writes the Monday, Friday and Sunday reports.

Lives in the API because this is the part that outlives a session. Claiming a
task is one conditional update on its `next_run_at`, so two machines on the same
database cannot both fire it; a machine that dies after the claim loses that one
run rather than repeating it, which is the right way round for a message.

Reminders and reports fire here, without a model. Work that takes one runs only
when the user scheduled it themselves (kind `work`): the clock queues it in
`brain_jobs`, the process on the user's PC does it, and the result comes back here
as a notice. Nothing starts on the assistant's own initiative.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.core.config import settings
from src.services import assistant_service, calendar_service, google_service
from src.services.channels import get_channel_by_kind
from src.services.credits import get_supabase_admin
from src.services.twilio_service import send_sms_message, send_whatsapp_message

logger = logging.getLogger("kwami-api")

MAX_DUE_PER_TICK = 50
#: A report about "this week" read days late is noise; one this stale is skipped.
REPORT_STALE_AFTER = timedelta(hours=12)
#: Twilio's limit is 1600. The rest stays in the app.
MAX_MESSAGE_CHARS = 1500
MORE_IN_APP = "\n\n(Resto en la app.)"
_DAY_NAMES = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

_scheduler_task: asyncio.Task[None] | None = None


def _rows(result: Any) -> list[dict[str, Any]]:
    return list(getattr(result, "data", None) or [])


def _instant(value: str, zone: ZoneInfo) -> tuple[datetime, bool]:
    """The moment and whether it has a time. An all-day event from Google is a bare date."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone), False
    return parsed.astimezone(zone), True


def _when(value: str, zone: ZoneInfo) -> str:
    moment, timed = _instant(value, zone)
    day = f"{_DAY_NAMES[moment.weekday()]} {moment:%d/%m}"
    return f"{day} {moment:%H:%M}" if timed else day


async def _events_between(
    user_id: str, kwami_id: str, start: datetime, end: datetime, zone: ZoneInfo
) -> tuple[list[str], list[str]]:
    """Lines for the kwami's calendar and the user's Google one, and what could not be read."""
    entries: list[tuple[str, str]] = []
    gaps: list[str] = []
    own = await calendar_service.list_events(user_id, kwami_id, start.isoformat(), end.isoformat())
    entries.extend((e["starts_at"], e["title"]) for e in own)
    if settings.google_enabled:
        try:
            external = await google_service.list_events(user_id, start.isoformat(), end.isoformat())
            entries.extend((e["starts_at"], e["title"]) for e in external)
        except google_service.GoogleNotConnectedError:
            pass
        except Exception:
            logger.exception("Could not read Google Calendar for a report")
            gaps.append("No he podido leer su Google Calendar.")
    entries.sort(key=lambda entry: _instant(entry[0], zone)[0])
    return [f"- {_when(at, zone)}: {title}" for at, title in entries], gaps


async def _reminders_between(
    user_id: str, kwami_id: str, start: datetime, end: datetime, zone: ZoneInfo
) -> list[str]:
    result = await (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .select("title, next_run_at")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("kind", "reminder")
        .eq("status", "open")
        .gte("next_run_at", start.isoformat())
        .lt("next_run_at", end.isoformat())
        .order("next_run_at", desc=False)
        .execute()
    )
    return [f"- {_when(row['next_run_at'], zone)}: {row['title']}" for row in _rows(result)]


async def _followups(
    user_id: str, kwami_id: str, status: str, since: datetime | None = None
) -> list[str]:
    query = (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .select("title, notes")
        .eq("user_id", user_id)
        .eq("kwami_id", kwami_id)
        .eq("kind", "followup")
        .eq("status", status)
    )
    if since is not None:
        query = query.gte("updated_at", since.isoformat())
    result = await query.order("created_at", desc=False).execute()
    return [
        f"- {row['title']}" + (f" ({row['notes']})" if row.get("notes") else "")
        for row in _rows(result)
    ]


def _section(heading: str, lines: list[str], empty: str) -> str:
    return f"{heading}\n" + ("\n".join(lines) if lines else empty)


async def build_report(
    report_type: str, user_id: str, kwami_id: str, timezone: str, now: datetime
) -> tuple[str, str]:
    """Title and text. Written from the data alone: no model, so nothing is made up."""
    zone = assistant_service.parse_zone(timezone)
    today = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)
    open_items = await _followups(user_id, kwami_id, "open")

    if report_type == "friday":
        done = await _followups(user_id, kwami_id, "done", since=monday)
        events, gaps = await _events_between(
            user_id, kwami_id, next_monday, next_monday + timedelta(days=7), zone
        )
        parts = [
            _section("Cerrado esta semana:", done, "Nada marcado como hecho."),
            _section("Sigue en marcha:", open_items, "Nada abierto."),
            _section("La semana que viene:", events, "Sin citas todavía."),
        ]
        return "Informe del viernes", "\n\n".join([*parts, *gaps])

    if report_type == "sunday":
        start, end = next_monday, next_monday + timedelta(days=7)
        title = "Recordatorio: la semana que viene"
    else:
        start, end = monday, next_monday
        title = "Informe del lunes"
    events, gaps = await _events_between(user_id, kwami_id, start, end, zone)
    reminders = await _reminders_between(user_id, kwami_id, start, end, zone)
    parts = [
        _section("Citas:", events, "Sin citas."),
        _section("Recordatorios:", reminders, "Ninguno."),
        _section("Tareas pendientes:", open_items, "Nada abierto."),
    ]
    return title, "\n\n".join([*parts, *gaps])


def _clip(body: str) -> str:
    if len(body) <= MAX_MESSAGE_CHARS:
        return body
    return body[: MAX_MESSAGE_CHARS - len(MORE_IN_APP)] + MORE_IN_APP


async def _send_over(kind: str, user_id: str, kwami_id: str, to: str, body: str) -> None:
    channel = await get_channel_by_kind(user_id, kwami_id, kind)
    if not channel:
        raise RuntimeError(f"No {kind} channel is configured")
    if kind == "sms":
        await send_sms_message(
            from_number=str(channel.get("phone_number") or ""), to_number=to, body=body
        )
        return
    sender = str(channel.get("provider_sender") or settings.twilio_whatsapp_from or "").strip()
    if not sender:
        raise RuntimeError("No WhatsApp sender is configured")
    await send_whatsapp_message(
        from_address=sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}",
        to_address=f"whatsapp:{to}",
        body=body,
    )


async def send_notify_code(user_id: str, kwami_id: str, notify_to: str, preferred: str) -> str:
    """Send the code that confirms a number. The proof for it goes back to the app."""
    number, code, proof = assistant_service.new_notify_code(user_id, kwami_id, notify_to)
    text = f"Su código de Kwami es {code}. Caduca en 10 minutos."
    for kind in [preferred, *[k for k in ("whatsapp", "sms") if k != preferred]]:
        try:
            await _send_over(kind, user_id, kwami_id, number, text)
            return proof
        except Exception as exc:
            logger.warning("Could not send a code over %s: %s", kind, type(exc).__name__)
    raise RuntimeError("The code could not be sent")


async def deliver(
    user_id: str, kwami_id: str, task_id: str | None, title: str, body: str
) -> dict[str, Any]:
    """The app always gets it. The phone gets it when it can, and a failure is on record.

    WhatsApp refuses free text outside its 24-hour window, which is the normal case
    for a Monday report, so a failure falls through to SMS rather than to silence.
    """
    row = await assistant_service.get_settings(user_id, kwami_id)
    delivered_via = "app"
    errors: list[str] = []
    to = row.get("notify_to")
    if to:
        preferred = row.get("notify_channel") or "whatsapp"
        text = _clip(f"{title}\n\n{body}")
        for kind in [preferred, *[k for k in ("whatsapp", "sms") if k != preferred]]:
            try:
                await _send_over(kind, user_id, kwami_id, to, text)
                delivered_via = kind
                errors = []
                break
            except Exception as exc:
                logger.warning("Could not deliver over %s: %s", kind, type(exc).__name__)
                errors.append(f"{kind}: {type(exc).__name__}")
    created = await (
        get_supabase_admin()
        .table("kwami_assistant_notifications")
        .insert(
            {
                "user_id": user_id,
                "kwami_id": kwami_id,
                "task_id": task_id,
                "title": title,
                "body": body,
                "delivered_via": delivered_via,
                "delivery_error": "; ".join(errors) or None,
            }
        )
        .execute()
    )
    return _rows(created)[0]


async def _claim(task: dict[str, Any], now: datetime) -> bool:
    next_run = assistant_service.next_occurrence(
        task["recurrence"],
        task.get("weekdays") or [],
        task.get("local_time"),
        task["timezone"],
        now,
    )
    payload: dict[str, Any] = {
        "next_run_at": next_run.isoformat() if next_run else None,
        "last_run_at": now.isoformat(),
    }
    if next_run is None:
        payload["status"] = "done"
    claimed = await (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .update(payload)
        .eq("id", task["id"])
        .eq("next_run_at", task["next_run_at"])
        .execute()
    )
    return bool(_rows(claimed))


async def fire(task: dict[str, Any], now: datetime) -> bool:
    if not await _claim(task, now):
        return False
    user_id, kwami_id = task["user_id"], task["kwami_id"]
    if task["kind"] == "work":
        job: dict[str, Any] = {
            "user_id": user_id,
            "kwami_id": kwami_id,
            "source": "schedule",
            "schedule_task_id": task["id"],
            "title": task["title"],
            "task": task["notes"],
        }
        # Named only when set (migration 029): the rest queues as it always did.
        if task.get("agent_id"):
            job["agent_id"] = task["agent_id"]
        if task.get("target") and task["target"] != "auto":
            job["target"] = task["target"]
        await get_supabase_admin().table("brain_jobs").insert(job).execute()
        return True
    if task["kind"] == "report":
        due = datetime.fromisoformat(task["next_run_at"].replace("Z", "+00:00"))
        if now - due > REPORT_STALE_AFTER:
            logger.info("Skipping a stale %s report", task["report_type"])
            return False
        title, body = await build_report(
            task["report_type"], user_id, kwami_id, task["timezone"], now
        )
    else:
        title, body = task["title"], task.get("notes") or task["title"]
    await deliver(user_id, kwami_id, task["id"], title, body)
    return True


async def notify_finished_work() -> int:
    """Tell the user how scheduled work, and work asked from the app, ended.

    Calls speak their own results. Work asked from the app has nobody on the line
    either -- typically it was the phone, with the PC off and the cloud doing it.

    Stamped before sending, as `_claim` does: a machine that dies in between loses
    one notice rather than repeating it, and the result stays in the next greeting.
    """
    result = await (
        get_supabase_admin()
        .table("brain_jobs")
        .select("id, user_id, kwami_id, schedule_task_id, title, status, result")
        .in_("source", ["schedule", "app"])
        .is_("notified_at", "null")
        .in_("status", ["done", "failed"])
        .limit(MAX_DUE_PER_TICK)
        .execute()
    )
    notified = 0
    for job in _rows(result):
        try:
            claimed = await (
                get_supabase_admin()
                .table("brain_jobs")
                .update({"notified_at": datetime.now(UTC).isoformat()})
                .eq("id", job["id"])
                .is_("notified_at", "null")
                .execute()
            )
            if not _rows(claimed):
                continue
            title = job["title"] if job["status"] == "done" else f"No terminado: {job['title']}"
            body = job.get("result") or "Sin resultado."
            await deliver(job["user_id"], job["kwami_id"], job.get("schedule_task_id"), title, body)
            notified += 1
        except Exception:
            logger.exception("Could not notify the end of job %s", job.get("id"))
    return notified


async def tick(now: datetime | None = None) -> int:
    """Fire everything due. One task failing never holds up the others."""
    moment = now or datetime.now(UTC)
    try:
        await notify_finished_work()
    except Exception:
        logger.exception("Could not read finished scheduled work")
    result = await (
        get_supabase_admin()
        .table("kwami_assistant_tasks")
        .select("*")
        .eq("status", "open")
        .lte("next_run_at", moment.isoformat())
        .order("next_run_at", desc=False)
        .limit(MAX_DUE_PER_TICK)
        .execute()
    )
    fired = 0
    for task in _rows(result):
        try:
            fired += await fire(task, moment)
        except Exception:
            logger.exception("Scheduled task %s failed", task.get("id"))
    return fired


async def _run_forever() -> None:
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("The assistant scheduler tick failed")
        await asyncio.sleep(settings.assistant_scheduler_interval_seconds)


def start_scheduler() -> bool:
    global _scheduler_task
    if not settings.assistant_scheduler_enabled:
        logger.info("⏰ Assistant scheduler: disabled")
        return False
    if not (settings.supabase_url and settings.supabase_secret_key):
        logger.warning("⏰ Assistant scheduler: no database, not started")
        return False
    _scheduler_task = asyncio.create_task(_run_forever(), name="assistant-scheduler")
    logger.info("⏰ Assistant scheduler: running")
    return True


async def stop_scheduler() -> None:
    global _scheduler_task
    task, _scheduler_task = _scheduler_task, None
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
