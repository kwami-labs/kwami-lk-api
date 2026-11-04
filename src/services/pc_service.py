"""What the app asks of the user's own PC: its state, and the orders that sign it in.

The orders go through `brain_jobs`, like any other work for the PC. The sign-ins
themselves happen there, on the user's screen: nothing they type passes through here.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from src.services.channels import get_owned_kwami, now_iso
from src.services.credits import get_supabase_admin

#: The same as the agent's RUNNER_FRESH_SECONDS: past this, its worker has gone.
RUNNER_FRESH = timedelta(seconds=90)
MAX_URL_LENGTH = 500


class PcOfflineError(Exception):
    """The user's PC is not there to carry the order out."""


def site_address(raw: str) -> str:
    """The address as typed, with its scheme. Raises ValueError for what is not a public site."""
    text = raw.strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or "." not in host or len(text) > MAX_URL_LENGTH:
        raise ValueError("That is not a web address")
    if host.endswith((".localhost", ".local")):
        raise ValueError("That is not a public site")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return text
    raise ValueError("That is not a public site")


async def status(user_id: str) -> dict[str, bool]:
    seen_since = (datetime.now(UTC) - RUNNER_FRESH).isoformat()
    result = await (
        get_supabase_admin()
        .table("brain_runners")
        .select("claude_login")
        .eq("user_id", user_id)
        .gte("last_seen_at", seen_since)
        .execute()
    )
    rows = cast(list[dict[str, Any]], result.data or [])
    return {"online": bool(rows), "claude_login": any(row.get("claude_login") for row in rows)}


async def _order(user_id: str, kwami_id: str, mode: str, title: str, task: str) -> str:
    await get_owned_kwami(user_id, kwami_id)
    if not (await status(user_id))["online"]:
        raise PcOfflineError
    order_id = str(uuid.uuid4())
    await (
        get_supabase_admin()
        .table("brain_jobs")
        .insert(
            {
                "id": order_id,
                "user_id": user_id,
                "kwami_id": kwami_id,
                "mode": mode,
                "title": title,
                "task": task,
                # The worker drops an order nobody is waiting for.
                "watcher_seen_at": now_iso(),
                "delivered_at": now_iso(),
            }
        )
        .execute()
    )
    return order_id


async def connect_claude(user_id: str, kwami_id: str) -> str:
    return await _order(user_id, kwami_id, "login", "Conectar la cuenta de Claude", "login")


async def show_site(user_id: str, kwami_id: str, address: str) -> str:
    url = site_address(address)
    host = str(urlsplit(url).hostname).lower()
    order_id = await _order(user_id, kwami_id, "site", f"Entrar en {host}", url)
    await (
        get_supabase_admin()
        .table("brain_sites")
        .upsert(
            {
                "user_id": user_id,
                "kwami_id": kwami_id,
                "host": host,
                "url": url,
                "state": "pending",
                "checked_at": now_iso(),
            },
            on_conflict="user_id,host",
        )
        .execute()
    )
    return order_id


async def list_sites(user_id: str) -> list[dict[str, Any]]:
    result = await (
        get_supabase_admin()
        .table("brain_sites")
        .select("host,url,state,checked_at")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    )
    return cast(list[dict[str, Any]], result.data or [])


async def forget_site(user_id: str, host: str) -> None:
    """Off the list. The session itself stays in Chrome until the site or the user ends it."""
    await (
        get_supabase_admin()
        .table("brain_sites")
        .delete()
        .eq("user_id", user_id)
        .eq("host", host.lower())
        .execute()
    )


async def order_state(user_id: str, order_id: str) -> dict[str, Any] | None:
    result = await (
        get_supabase_admin()
        .table("brain_jobs")
        .select("status,result")
        .eq("user_id", user_id)
        .eq("id", order_id)
        .in_("mode", ["login", "site"])
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], result.data or [])
    return rows[0] if rows else None
