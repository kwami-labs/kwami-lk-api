"""Liveness and readiness."""

from __future__ import annotations

import logging

import anyio
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src import __version__
from src.core.config import settings

router = APIRouter()
logger = logging.getLogger("kwami-api.health")

# A readiness check that can hang is worse than no readiness check: the platform
# waits on it instead of restarting the instance.
DEPENDENCY_TIMEOUT_SECONDS = 2.0


@router.get("/health")
async def health_check():
    """Liveness: is the process up?

    Deliberately checks nothing external. A dependency outage must not make every
    instance look dead and trigger a restart loop -- that is what readiness is
    for. Fly's `[[http_service.checks]]` points here.
    """
    return {"status": "healthy", "service": "kwami-lk-api"}


async def _check_supabase() -> None:
    from src.services.credits import get_supabase_admin

    sb = get_supabase_admin()
    await sb.table("user_credits").select("user_id").limit(1).execute()


async def _check_zep() -> None:
    from src.api.routes.memory import get_zep_client

    client = await get_zep_client()
    await client.thread.list_all(page_number=1, page_size=1)


async def _probe(name: str, check) -> tuple[str, dict[str, object]]:
    """Run one dependency check under a timeout, reporting rather than raising."""
    try:
        with anyio.fail_after(DEPENDENCY_TIMEOUT_SECONDS):
            await check()
    except TimeoutError:
        return name, {"ok": False, "error": "timeout"}
    except Exception as exc:  # a readiness probe reports, it does not propagate
        return name, {"ok": False, "error": type(exc).__name__}
    return name, {"ok": True}


@router.get("/health/ready")
async def readiness_check():
    """Readiness: can this instance actually serve traffic?

    `/health` returns a static dict, so an instance that cannot reach Supabase
    still looked healthy and kept receiving requests. This checks the
    dependencies a request actually needs, concurrently and under a short
    timeout, and answers 503 when one of them is down so the platform stops
    routing here.

    A dependency that is not configured is not a failure -- a deployment with no
    Zep key is a supported deployment, and it is `not_configured`, not unready.
    """
    checks: dict[str, dict[str, object]] = {}

    async def run(name, check):
        key, result = await _probe(name, check)
        checks[key] = result

    async with anyio.create_task_group() as group:
        if settings.supabase_url and settings.supabase_secret_key:
            group.start_soon(run, "supabase", _check_supabase)
        else:
            checks["supabase"] = {"ok": True, "status": "not_configured"}
        if settings.zep_api_key:
            group.start_soon(run, "zep", _check_zep)
        else:
            checks["zep"] = {"ok": True, "status": "not_configured"}

    ready = all(check["ok"] for check in checks.values())
    body = {"status": "ready" if ready else "not_ready", "checks": checks}
    if not ready:
        logger.warning("readiness check failed", extra={"checks": checks})
        return JSONResponse(status_code=503, content=body)
    return body


@router.get("/")
async def root():
    """Root endpoint with API info.

    The version is read from ``src.__version__`` rather than written here. This
    literal said "0.1.0" while the package said "0.1.1": `scripts/set-version.sh`
    rewrites `src/__init__.py`, `pyproject.toml`, `uv.lock` and the README badge
    during a release, and never touched this string, so it drifted one release
    further behind every time.

    `docs` is null when the UIs are closed, rather than advertising a 404.
    """
    return {
        "name": "Kwami AI LiveKit API",
        "version": __version__,
        "docs": "/docs" if settings.show_docs else None,
    }
