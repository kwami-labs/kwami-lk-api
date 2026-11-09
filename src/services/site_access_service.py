"""Where Jarvis may go on the web, and what it may do on each site.

The user keeps a list of sites, each with one level of access, and one level for
every other site. Jarvis can ask for a site it has no rule for; the request waits
until the user answers it here, and changes nothing before that.

Levels, each including the ones before it:

* ``blocked``: never opened.
* ``read``: opened and read.
* ``browse``: also clicked through, scrolled and searched.
* ``write``: also typed into and forms sent.
* ``account``: also the user's signed-in session, sign-in forms included.

The agent applies the same rules (``agent/src/browser/access.py``): a pattern
covers its subdomains, and the longest pattern that matches decides. Without any
rule every site is at ``account``, which is how Jarvis worked before this list.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any, cast
from urllib.parse import urlsplit

from src.services.credits import get_supabase_admin

logger = logging.getLogger("kwami-api")

TABLE = "jarvis_site_rules"
LEVELS = ("blocked", "read", "browse", "write", "account")
#: The pattern of the row that holds the level for every other site.
EVERY_OTHER_SITE = "*"
#: What every other site gets until the user says otherwise.
DEFAULT_LEVEL = "account"
MAX_RULES = 200
MAX_ADDRESS_CHARS = 2048

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class InvalidSiteError(ValueError):
    """Not an address or a host a rule can be kept for."""


class TooManySitesError(ValueError):
    """The list is full."""


class UnknownLevelError(ValueError):
    """Not one of ``LEVELS``."""


def check_level(access: str) -> str:
    if access not in LEVELS:
        raise UnknownLevelError(access)
    return access


def site_pattern(address: str) -> str:
    """The host a rule is kept for: ``https://www.GitHub.com/x`` -> ``github.com``.

    A leading ``*.`` is dropped, since a pattern covers its subdomains anyway, and
    so is ``www.``. Raises ``InvalidSiteError`` for anything that is not a host.
    """
    raw = (address or "").strip()
    if not raw or len(raw) > MAX_ADDRESS_CHARS:
        raise InvalidSiteError("empty or too long")
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError as exc:
        raise InvalidSiteError("not an address") from exc
    host = host.strip(".")
    if host.startswith("*."):
        host = host[2:]
    if host.startswith("www."):
        host = host[4:]
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidSiteError("not a host name") from exc
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253 or not all(_LABEL.match(label) for label in labels):
        raise InvalidSiteError("not a host name")
    return host


def level_for(host: str, rules: dict[str, str], default: str = DEFAULT_LEVEL) -> str:
    """The level for ``host``: the longest pattern that is it or a parent of it."""
    host = host.lower().strip(".")
    best = ""
    for pattern in rules:
        if (host == pattern or host.endswith(f".{pattern}")) and len(pattern) > len(best):
            best = pattern
    return rules[best] if best else default


async def _rows(user_id: str) -> list[dict[str, Any]]:
    result = await (
        get_supabase_admin()
        .table(TABLE)
        .select("id, pattern, access, status, reason, created_at, updated_at")
        .eq("user_id", user_id)
        .order("pattern")
        .execute()
    )
    return cast(list[dict[str, Any]], result.data or [])


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "pattern": row["pattern"],
        "access": row["access"],
        "reason": row.get("reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def list_access(user_id: str) -> dict[str, Any]:
    """The list as the app shows it: the default, the sites, and Jarvis's requests."""
    default = DEFAULT_LEVEL
    sites: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for row in await _rows(user_id):
        if row["pattern"] == EVERY_OTHER_SITE:
            if row["status"] == "active":
                default = row["access"]
        elif row["status"] == "pending":
            pending.append(_public(row))
        else:
            sites.append(_public(row))
    return {"default": default, "sites": sites, "pending": pending, "levels": list(LEVELS)}


async def check(user_id: str, address: str) -> dict[str, str]:
    """What Jarvis may do at ``address``, and the pattern that decides it."""
    host = site_pattern(address)
    rules: dict[str, str] = {}
    default = DEFAULT_LEVEL
    for row in await _rows(user_id):
        if row["status"] != "active":
            continue
        if row["pattern"] == EVERY_OTHER_SITE:
            default = row["access"]
        else:
            rules[row["pattern"]] = row["access"]
    access = level_for(host, rules, default)
    matched = max((p for p in rules if host == p or host.endswith(f".{p}")), key=len, default="")
    return {"host": host, "access": access, "pattern": matched or EVERY_OTHER_SITE}


async def _upsert(user_id: str, pattern: str, access: str) -> dict[str, Any]:
    result = await (
        get_supabase_admin()
        .table(TABLE)
        .upsert(
            {
                "user_id": user_id,
                "pattern": pattern,
                "access": access,
                "status": "active",
                "reason": None,
            },
            on_conflict="user_id,pattern",
        )
        .execute()
    )
    rows = cast(list[dict[str, Any]], result.data or [])
    return _public(rows[0])


async def set_site(user_id: str, address: str, access: str) -> dict[str, Any]:
    """Keep ``access`` for the site at ``address``, replacing what it had."""
    check_level(access)
    pattern = site_pattern(address)
    rows = await _rows(user_id)
    known = {row["pattern"] for row in rows}
    if pattern not in known and len(known - {EVERY_OTHER_SITE}) >= MAX_RULES:
        raise TooManySitesError(f"at most {MAX_RULES} sites")
    return await _upsert(user_id, pattern, access)


async def set_default(user_id: str, access: str) -> str:
    """The level for every site without a rule of its own."""
    check_level(access)
    await _upsert(user_id, EVERY_OTHER_SITE, access)
    return access


async def remove_site(user_id: str, rule_id: str) -> bool:
    """Forget a site. The row for every other site is never removed here."""
    result = await (
        get_supabase_admin()
        .table(TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("id", rule_id)
        .neq("pattern", EVERY_OTHER_SITE)
        .execute()
    )
    return bool(result.data)


async def answer_request(
    user_id: str, rule_id: str, approve: bool, access: str | None = None
) -> dict[str, Any] | None:
    """Approve Jarvis's request, at the level it asked for or ``access``, or refuse it.

    Refusing forgets the request, so Jarvis can ask again another day. Returns the
    rule kept, or None when refused or when there was no such request.
    """
    if access is not None:
        check_level(access)
    table = get_supabase_admin().table(TABLE)
    if not approve:
        await (
            table.delete()
            .eq("user_id", user_id)
            .eq("id", rule_id)
            .eq("status", "pending")
            .execute()
        )
        return None
    change: dict[str, Any] = {"status": "active"}
    if access is not None:
        change["access"] = access
    result = await (
        table.update(change)
        .eq("user_id", user_id)
        .eq("id", rule_id)
        .eq("status", "pending")
        .execute()
    )
    rows = cast(list[dict[str, Any]], result.data or [])
    return _public(rows[0]) if rows else None
