"""The brain from the app: where it can work, the work it is asked, and its agents.

The PC's process and the cloud's both take jobs from `brain_jobs` (migration 029
decides who takes which). This is the app's way in: on the phone or the PC, with
no call behind it, the user asks for work, says whether the cloud may do it while
the PC is off, defines the agents the brain hands tasks to, and sees what the
cloud made that the PC still has to bring home (`brain_sync`).

Only the service role writes these tables. Everything here is scoped to the user
in the token, and a kwami must be theirs before anything is queued for it.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from postgrest.exceptions import APIError

from src.services.channels import get_owned_kwami, now_iso
from src.services.credits import get_supabase_admin
from src.services.pc_service import RUNNER_FRESH

Target = Literal["auto", "pc", "cloud"]
Where = Literal["pc", "cloud", "waiting"]

#: A cloud worker beats every 30 s; past this it has gone (the agent's CLOUD_FRESH_SECONDS).
CLOUD_FRESH = timedelta(seconds=90)
#: Work queued from the app and not started yet. Past it, finish or cancel some first.
MAX_QUEUED = 20
MAX_TASK_CHARS = 8000
MAX_TITLE_CHARS = 200
MAX_LIST = 50

#: The models an agent may run on: the executor's own team (`brain/team.py` there).
AGENT_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
#: Never more than the executor itself has (`team.OWN_MEMBER_TOOLS` there).
AGENT_TOOLS = ("Read", "Glob", "Grep", "WebSearch", "WebFetch", "Write", "Edit", "Bash")
AGENT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
MAX_AGENTS = 30

#: What the app shows of a job. Not `task`: it can carry the user's profile.
JOB_COLUMNS = (
    "id, kwami_id, thread_id, source, title, status, target, agent_id, progress, result, "
    "cost_usd, runner_id, created_at, started_at, finished_at"
)
SYNC_COLUMNS = (
    "id, kwami_id, job_id, kind, repo, files, title, status, result, created_at, applied_at"
)
#: PostgREST and Postgres saying the table or column is not there: migration 029 is missing.
_NOT_MIGRATED = frozenset({"42P01", "42703", "PGRST204", "PGRST205"})


class BrainNotReadyError(Exception):
    """The database has not had migration 029 yet."""


class TooManyJobsError(Exception):
    """The user already has `MAX_QUEUED` jobs waiting."""


class AgentNotFoundError(Exception):
    """No such agent, or not this user's."""


def _rows(result: Any) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], getattr(result, "data", None) or [])


def _since(age: timedelta) -> str:
    return (datetime.now(UTC) - age).isoformat()


async def _run(query: Any) -> list[dict[str, Any]]:
    """Execute, telling a database without migration 029 from any other failure."""
    try:
        return _rows(await query.execute())
    except APIError as exc:
        if exc.code in _NOT_MIGRATED:
            raise BrainNotReadyError from exc
        raise


# -- where the brain can work ---------------------------------------------------


async def pc_online(user_id: str) -> tuple[bool, bool]:
    """(online, signed in to Claude) for the user's PC."""
    rows = await _run(
        get_supabase_admin()
        .table("brain_runners")
        .select("claude_login")
        .eq("user_id", user_id)
        .gte("last_seen_at", _since(RUNNER_FRESH))
    )
    return bool(rows), any(row.get("claude_login") for row in rows)


async def cloud_online() -> bool:
    rows = await _run(
        get_supabase_admin()
        .table("brain_cloud_runners")
        .select("runner_id")
        .gte("last_seen_at", _since(CLOUD_FRESH))
        .limit(1)
    )
    return bool(rows)


async def cloud_enabled(user_id: str) -> bool:
    rows = await _run(
        get_supabase_admin()
        .table("brain_cloud_settings")
        .select("enabled")
        .eq("user_id", user_id)
        .limit(1)
    )
    return bool(rows and rows[0].get("enabled"))


async def set_cloud(user_id: str, enabled: bool) -> bool:
    await _run(
        get_supabase_admin()
        .table("brain_cloud_settings")
        .upsert(
            {"user_id": user_id, "enabled": enabled, "updated_at": now_iso()},
            on_conflict="user_id",
        )
    )
    return enabled


async def pending_sync(user_id: str) -> int:
    rows = await _run(
        get_supabase_admin()
        .table("brain_sync")
        .select("id")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .limit(MAX_LIST)
    )
    return len(rows)


def placement(target: str, pc: bool, cloud: bool, allowed: bool) -> Where:
    """Who will take a job, as the claim functions of migration 029 decide it.

    `cloud` is a worker being there; `allowed` the user's consent. Without both, work
    for the cloud waits, and so does work for a PC that is off.
    """
    if target == "cloud":
        return "cloud" if cloud and allowed else "waiting"
    if pc:
        return "pc"
    if target == "auto" and cloud and allowed:
        return "cloud"
    return "waiting"


async def where(user_id: str) -> dict[str, Any]:
    online, login = await pc_online(user_id)
    cloud = await cloud_online()
    allowed = await cloud_enabled(user_id)
    return {
        "pc": {"online": online, "claude_login": login},
        "cloud": {"online": cloud, "enabled": allowed},
        "sync": {"pending": await pending_sync(user_id)},
        "auto": placement("auto", online, cloud, allowed),
    }


# -- work asked from the app ------------------------------------------------------


def _title(task: str, title: str) -> str:
    """The title given, else the task's first line; the list shows nothing longer."""
    text = (title or task).strip()
    first = text.splitlines()[0].strip() if text else ""
    return first[:MAX_TITLE_CHARS] or "Encargo"


async def _queued(user_id: str) -> int:
    rows = await _run(
        get_supabase_admin()
        .table("brain_jobs")
        .select("id")
        .eq("user_id", user_id)
        .eq("status", "queued")
        .limit(MAX_QUEUED + 1)
    )
    return len(rows)


async def queue_job(
    user_id: str,
    kwami_id: str,
    task: str,
    *,
    title: str = "",
    target: Target = "auto",
    difficulty: str = "normal",
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Queue work for whoever takes it, and say who that will be.

    Raises ValueError for a kwami that is not the user's, AgentNotFoundError for an
    agent that is not theirs, and TooManyJobsError past `MAX_QUEUED`.
    """
    await get_owned_kwami(user_id, kwami_id)
    if agent_id is not None:
        agent = await get_agent(user_id, agent_id)
        if target == "auto":
            target = cast(Target, agent.get("target") or "auto")
    if await _queued(user_id) >= MAX_QUEUED:
        raise TooManyJobsError
    job_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
    row: dict[str, Any] = {
        "id": job_id,
        "user_id": user_id,
        "kwami_id": kwami_id,
        "thread_id": thread_id,
        "source": "app",
        "title": _title(task, title),
        "task": task.strip(),
        "difficulty": difficulty,
        "target": target,
        "status": "queued",
        "mode": "work",
        "created_at": now_iso(),
    }
    if agent_id is not None:
        row["agent_id"] = agent_id
    await _run(get_supabase_admin().table("brain_jobs").insert(row))
    online, _ = await pc_online(user_id)
    going = placement(target, online, await cloud_online(), await cloud_enabled(user_id))
    return {"id": job_id, "thread_id": thread_id, "target": target, "where": going}


async def list_jobs(
    user_id: str, kwami_id: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    query = (
        get_supabase_admin()
        .table("brain_jobs")
        .select(JOB_COLUMNS)
        .eq("user_id", user_id)
        .eq("mode", "work")
    )
    if kwami_id:
        query = query.eq("kwami_id", kwami_id)
    rows = await _run(query.order("created_at", desc=True).limit(min(max(limit, 1), MAX_LIST)))
    for row in rows:
        runner = str(row.pop("runner_id", "") or "")
        row["ran_on"] = "cloud" if runner.startswith("cloud-") else ("pc" if runner else "")
    return rows


async def cancel_job(user_id: str, job_id: str) -> bool:
    """Cancel it: a queued one here, a running one on its worker's next look."""
    queued = await _run(
        get_supabase_admin()
        .table("brain_jobs")
        .update({"status": "cancelled", "finished_at": now_iso(), "result": "Cancelada."})
        .eq("id", job_id)
        .eq("user_id", user_id)
        .eq("status", "queued")
    )
    running = await _run(
        get_supabase_admin()
        .table("brain_jobs")
        .update({"cancel_requested": True})
        .eq("id", job_id)
        .eq("user_id", user_id)
        .eq("status", "running")
    )
    return bool(queued or running)


# -- the user's agents ------------------------------------------------------------


def clean_agent(fields: dict[str, Any]) -> dict[str, Any]:
    """The fields that may be stored, checked. Raises ValueError naming the bad one."""
    out: dict[str, Any] = {}
    if "name" in fields:
        name = str(fields["name"]).strip().lower()
        if not AGENT_NAME.fullmatch(name):
            raise ValueError("name: lowercase letters, digits and dashes, 2 to 40")
        out["name"] = name
    for key, limit in (("description", 500), ("prompt", 8000)):
        if key in fields:
            text = str(fields[key]).strip()
            if not 1 <= len(text) <= limit:
                raise ValueError(f"{key}: 1 to {limit} characters")
            out[key] = text
    if "model" in fields:
        if fields["model"] not in AGENT_MODELS:
            raise ValueError(f"model: one of {', '.join(AGENT_MODELS)}")
        out["model"] = fields["model"]
    if "tools" in fields:
        tools = list(dict.fromkeys(fields["tools"] or []))
        unknown = [t for t in tools if t not in AGENT_TOOLS]
        if unknown:
            raise ValueError(f"tools: unknown {', '.join(unknown)}")
        out["tools"] = tools
    if "effort" in fields:
        if fields["effort"] not in ("low", "medium", "high"):
            raise ValueError("effort: low, medium or high")
        out["effort"] = fields["effort"]
    if "target" in fields:
        if fields["target"] not in ("auto", "pc", "cloud"):
            raise ValueError("target: auto, pc or cloud")
        out["target"] = fields["target"]
    if "enabled" in fields:
        out["enabled"] = bool(fields["enabled"])
    return out


async def list_agents(user_id: str) -> list[dict[str, Any]]:
    return await _run(
        get_supabase_admin()
        .table("brain_agents")
        .select("*")
        .eq("user_id", user_id)
        .order("name", desc=False)
        .limit(MAX_AGENTS)
    )


async def get_agent(user_id: str, agent_id: str) -> dict[str, Any]:
    try:
        uuid.UUID(agent_id)
    except ValueError as exc:
        raise AgentNotFoundError from exc
    rows = await _run(
        get_supabase_admin()
        .table("brain_agents")
        .select("*")
        .eq("id", agent_id)
        .eq("user_id", user_id)
        .limit(1)
    )
    if not rows:
        raise AgentNotFoundError
    return rows[0]


async def create_agent(user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Raises ValueError for a bad field, a name in use or one agent too many."""
    clean = clean_agent(fields)
    if not {"name", "description", "prompt"} <= clean.keys():
        raise ValueError("name, description and prompt are needed")
    existing = await list_agents(user_id)
    if any(agent["name"] == clean["name"] for agent in existing):
        raise ValueError("name: already in use")
    if len(existing) >= MAX_AGENTS:
        raise ValueError(f"no more than {MAX_AGENTS} agents")
    now = now_iso()
    row = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "model": AGENT_MODELS[1],
        "tools": ["Read", "Glob", "Grep"],
        "effort": "medium",
        "target": "auto",
        "enabled": True,
        **clean,
        "created_at": now,
        "updated_at": now,
    }
    await _run(get_supabase_admin().table("brain_agents").insert(row))
    return row


async def update_agent(user_id: str, agent_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    current = await get_agent(user_id, agent_id)
    clean = clean_agent(fields)
    renamed = "name" in clean and clean["name"] != current["name"]
    if renamed and any(a["name"] == clean["name"] for a in await list_agents(user_id)):
        raise ValueError("name: already in use")
    if not clean:
        return current
    clean["updated_at"] = now_iso()
    await _run(
        get_supabase_admin()
        .table("brain_agents")
        .update(clean)
        .eq("id", agent_id)
        .eq("user_id", user_id)
    )
    return {**current, **clean}


async def delete_agent(user_id: str, agent_id: str) -> None:
    await get_agent(user_id, agent_id)
    await _run(
        get_supabase_admin()
        .table("brain_agents")
        .delete()
        .eq("id", agent_id)
        .eq("user_id", user_id)
    )


async def run_agent(user_id: str, agent_id: str, kwami_id: str, task: str) -> dict[str, Any]:
    """Queue a job for one agent. Without words of its own, the agent's prompt is the task."""
    agent = await get_agent(user_id, agent_id)
    if not agent.get("enabled", True):
        raise ValueError("agent: it is switched off")
    words = task.strip() or f"Haz tu trabajo de siempre: {agent['description']}"
    return await queue_job(
        user_id,
        kwami_id,
        words,
        title=f"{agent['name']}: {words}",
        agent_id=agent_id,
    )


# -- what the cloud left for the PC ---------------------------------------------------


async def list_sync(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return await _run(
        get_supabase_admin()
        .table("brain_sync")
        .select(SYNC_COLUMNS)
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(min(max(limit, 1), MAX_LIST))
    )


async def discard_sync(user_id: str, sync_id: str) -> bool:
    """Leave something the cloud made where it is, in Storage. Only while it waits."""
    rows = await _run(
        get_supabase_admin()
        .table("brain_sync")
        .update({"status": "discarded", "result": "Descartado desde la app."})
        .eq("id", sync_id)
        .eq("user_id", user_id)
        .eq("status", "pending")
    )
    return bool(rows)
