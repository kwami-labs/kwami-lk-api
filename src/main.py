"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from src.core.config import settings
from src.core.errors import install_error_handlers

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("kwami-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info(f"🚀 Starting {settings.app_name} v0.1.0")
    logger.info(f"🌐 Listening on {settings.api_host}:{settings.api_port}")
    logger.info(f"📡 LiveKit URL: {settings.livekit_url}")
    logger.info(f"🌍 Environment: {settings.app_env}")
    if settings.kwami_api_key and settings.kwami_api_key.strip():
        logger.info("📊 Kwami API key for usage report: set")
    else:
        logger.warning(
            "📊 Kwami API key for usage report: NOT SET (agent usage reports will get 503)"
        )
    yield
    logger.info("👋 Shutting down...")


app = FastAPI(
    title="Kwami AI LiveKit API",
    description="Token endpoint and configuration API for Kwami AI agents",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.show_docs else None,
    redoc_url="/redoc" if settings.show_docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
