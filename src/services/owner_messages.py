"""A message from a kwami's owner to the kwami's own number is something to do.

The owner is whoever confirmed their number for the kwami's notices
(`kwami_assistant_settings.notify_to`, confirmed with a code sent to it), so a
stranger writing to the number is only recorded, as before. What the owner writes
becomes work for the brain, as if asked from the app: the PC takes it when it is on,
the cloud when it is off and the user allowed it, and `notify_finished_work` sends
the result back to the same number when it ends.
"""

from __future__ import annotations

import logging
from typing import Any

from src.services import assistant_service, brain_service
from src.services.credits import get_supabase_admin

logger = logging.getLogger("kwami-api.owner-messages")

#: How the queued work is called back to the owner; the list shows nothing longer.
_REPLY_TITLE_CHARS = 60


async def is_redelivery(message_sid: str | None) -> bool:
    """Whether this message was already stored: Twilio redelivers on any failure."""
    if not message_sid:
        return False
    result = await (
        get_supabase_admin()
        .table("kwami_message_events")
        .select("id")
        .eq("provider_message_sid", message_sid)
        .limit(1)
        .execute()
    )
    return bool(getattr(result, "data", None))


async def owner_number(channel: dict[str, Any]) -> str | None:
    """The number the kwami's owner confirmed for its notices, if any.

    A failure to read it is no owner: the call or message still goes through, as a
    stranger's would, rather than failing the webhook into Twilio's retries.
    """
    try:
        row = await assistant_service.get_settings(channel["user_id"], channel["kwami_id"])
    except Exception:
        logger.exception("Could not read the owner's number")
        return None
    return row.get("notify_to") or None


def _reply(title: str, where: str) -> str:
    short = title if len(title) <= _REPLY_TITLE_CHARS else title[: _REPLY_TITLE_CHARS - 1] + "…"
    if where == "waiting":
        return (
            f"Anotado: «{short}». Ahora no hay dónde hacerlo (el PC está apagado y la nube "
            "no está disponible); empiezo en cuanto lo haya y te escribo al terminar."
        )
    return f"Recibido. Me pongo con «{short}» y te escribo al terminar."


async def owner_task_reply(channel: dict[str, Any], sender: str | None, body: str) -> str | None:
    """Queue what the owner wrote and say so; None when it is not the owner writing.

    `sender` is the writer's number, already in E.164.
    """
    text = (body or "").strip()
    if not text or not sender:
        return None
    if sender != await owner_number(channel):
        return None
    user_id, kwami_id = channel["user_id"], channel["kwami_id"]
    try:
        job = await brain_service.queue_job(user_id, kwami_id, text)
    except brain_service.TooManyJobsError:
        return "Tengo demasiados encargos en cola. Te aviso de los que acaben y vuelve a pedírmelo."
    logger.info("Queued work %s from the owner's message", job["id"])
    first_line = text.splitlines()[0].strip()
    return _reply(first_line, str(job.get("where") or ""))
