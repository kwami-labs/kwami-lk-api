"""FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from src import __version__
from src.api.docs import install_docs
from src.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from src.api.openapi_meta import CONTACT, DESCRIPTION, LICENSE_INFO, TAGS
from src.api.ratelimit import limiter, rate_limit_exceeded_handler
from src.api.routes import (
    access,
    admin_reconciliation,
    assistant,
    brain,
    calendar,
    channels,
    contacts,
    credits,
    email,
    google,
    google_workspace,
    health,
    internal,
    languages,
    memory,
    models,
    pc,
    providers,
    token,
    voices,
    wallet,
    webhooks,
)
from src.api.routes.memory import close_zep_client
from src.core.config import settings
from src.core.errors import install_error_handlers
from src.core.logging import configure_logging
from src.services.assistant_scheduler import start_scheduler, stop_scheduler
from src.services.credits import init_supabase_admin
from src.services.twilio_service import close_twilio_client
from src.services.voices import get_tts_voices, import_voice_plugins

# JSON records carrying the request id, so a reported error can be found.
configure_logging(debug=settings.debug, json_output=settings.log_json)
logger = logging.getLogger("kwami-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("🚀 Starting %s v%s", settings.app_name, __version__)
    logger.info("🌐 Listening on %s:%s", settings.api_host, settings.api_port)
    logger.info("📡 LiveKit URL: %s", settings.livekit_url)
    logger.info("🌍 Environment: %s", settings.app_env)
    if settings.kwami_api_key and settings.kwami_api_key.strip():
        logger.info("📊 Kwami API key for usage report: set")
    else:
        logger.warning(
            "📊 Kwami API key for usage report: NOT SET (agent usage reports will get 503)"
        )

    # One async Supabase client for the process, built here because
    # `create_async_client` is a coroutine and because a client per request is a
    # connection pool per request. Skipped when Supabase is not configured -- a
    # development run with no database still has to boot, and every route that
    # needs one raises a clear error instead.
    if settings.supabase_url and settings.supabase_secret_key:
        await init_supabase_admin()
        logger.info("🗄️  Supabase async client: ready")
    else:
        logger.warning("🗄️  Supabase not configured; database-backed routes will fail")

    start_scheduler()

    # The catalogue imports a heavy plugin: seconds with every request waiting, on whoever
    # opened the voice panel first. So before anybody asks, here because a plugin only
    # registers on the main thread, and the rest off the event loop.
    import_voice_plugins()
    app.state.voices_warm = asyncio.create_task(asyncio.to_thread(get_tts_voices))

    yield

    await app.state.voices_warm
    await stop_scheduler()
    await close_zep_client()
    await close_twilio_client()
    logger.info("👋 Shutting down...")


def docs_urls(show_docs: bool) -> dict[str, str | None]:
    """The schema and the pages that render it are one decision, not four.

    Gating only the pages left `/openapi.json` served in production with `ENABLE_DOCS=false` --
    the same route map, parameters and models the UIs render, minus the HTML. The pages themselves
    (`/docs`, `/swagger`, `/redoc`) are mounted by `install_docs`, not by FastAPI, so they are
    switched off here by simply not being installed; FastAPI's own `docs_url` and `redoc_url` are
    always `None`, because its built-in pages are blocked by the API's Content-Security-Policy.
    A function rather than ternaries so a test can assert on it directly.
    """
    return {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": "/openapi.json" if show_docs else None,
    }


app = FastAPI(
    title="Kwami AI LiveKit API",
    summary="Token endpoint and configuration API for Kwami AI agents",
    description=DESCRIPTION,
    version=__version__,
    contact=CONTACT,
    license_info=LICENSE_INFO,
    openapi_tags=TAGS,
    lifespan=lifespan,
    **docs_urls(settings.show_docs),
)

# Middleware runs outermost-last: Starlette applies these in reverse, so the
# request context is established first and therefore covers everything below
# it, including the rate limiter's rejections and the CORS preflight.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestContextMiddleware)

# `errors.py` has mapped 429 to `rate_limited` since it was written; this is the
# implementation that finally raises one.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Registered before the routers so every route is covered, including the
# catch-all that stops `detail=str(e)` forwarding upstream text to clients.
install_error_handlers(app)

app.include_router(health.router, tags=["Health"])
app.include_router(token.router, prefix="/token", tags=["Token"])
app.include_router(memory.router, prefix="/memory", tags=["Memory"])
app.include_router(models.router, prefix="/models", tags=["Models"])
app.include_router(voices.router, prefix="/voices", tags=["Voices"])
app.include_router(languages.router, prefix="/languages", tags=["Languages"])
app.include_router(credits.router, prefix="/credits", tags=["Credits"])
app.include_router(channels.router, prefix="/channels", tags=["Channels"])
app.include_router(contacts.router, prefix="/contacts", tags=["Contacts"])
app.include_router(wallet.router, prefix="/wallets", tags=["Wallets"])
app.include_router(email.router, prefix="/email", tags=["Email"])
app.include_router(calendar.router, prefix="/calendar", tags=["Calendar"])
app.include_router(assistant.router, prefix="/assistant", tags=["Assistant"])
app.include_router(google.router, prefix="/google", tags=["Google"])
app.include_router(google_workspace.router, prefix="/google", tags=["Google"])
app.include_router(pc.router, prefix="/pc", tags=["PC"])
app.include_router(brain.router, prefix="/brain", tags=["Brain"])
app.include_router(providers.router, prefix="/providers", tags=["Providers"])
app.include_router(access.router, prefix="/access", tags=["Access"])
app.include_router(internal.router, prefix="/internal", tags=["Internal"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks"])
app.include_router(
    admin_reconciliation.router,
    prefix="/admin/reconciliation",
    tags=["Admin Reconciliation"],
)

if settings.show_docs:
    install_docs(app)


def run():
    """Run the API server."""
    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        # One worker cannot use more than one core, and it is also a single point
        # of failure: a restart drops every request in flight. Reload mode is
        # single-worker by definition, so this only applies to a real run.
        workers=None if settings.debug else settings.web_concurrency,
        log_level="debug" if settings.debug else "info",
        # The Cloudflare Worker terminates TLS and proxies here. Without these,
        # request.url.scheme stays "http" (breaking Twilio signature validation,
        # which signs the https URL) and request.client.host is the proxy
        # rather than the caller. Only the Worker can reach this port.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
