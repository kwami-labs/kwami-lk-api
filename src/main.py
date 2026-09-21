"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from src import __version__
from src.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from src.api.ratelimit import limiter, rate_limit_exceeded_handler
from src.api.routes import (
    admin_reconciliation,
    calendar,
    channels,
    contacts,
    credits,
    email,
    health,
    internal,
    languages,
    memory,
    models,
    token,
    voices,
    wallet,
    webhooks,
)
from src.api.routes.memory import close_zep_client
from src.core.config import settings
from src.core.errors import install_error_handlers
from src.core.logging import configure_logging
from src.services.credits import init_supabase_admin
from src.services.twilio_service import close_twilio_client

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

    yield

    await close_zep_client()
    await close_twilio_client()
    logger.info("👋 Shutting down...")


def docs_urls(show_docs: bool) -> dict[str, str | None]:
    """The two UIs and the schema they fetch are one decision, not three.

    Gating only `docs_url` and `redoc_url` left `/openapi.json` served in
    production with `ENABLE_DOCS=false` -- the same route map, parameters and
    models the UIs render, minus the HTML. Returning all three together is what
    stops the next edit from closing one and leaving another open; it is a
    function rather than three ternaries so a test can assert on it directly.
    """
    if not show_docs:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


app = FastAPI(
    title="Kwami AI LiveKit API",
    description="Token endpoint and configuration API for Kwami AI agents",
    version=__version__,
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
app.include_router(internal.router, prefix="/internal", tags=["Internal"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks"])
app.include_router(
    admin_reconciliation.router,
    prefix="/admin/reconciliation",
    tags=["Admin Reconciliation"],
)


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
        # Fly terminates TLS and forwards over the internal network. Without
        # these, request.url.scheme stays "http" (breaking Twilio signature
        # validation, which signs the https URL) and request.client.host is the
        # proxy rather than the caller. Only the Fly proxy can reach this port.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
