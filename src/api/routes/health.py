"""Health check endpoints."""

from fastapi import APIRouter

from src import __version__
from src.core.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    return {"status": "healthy", "service": "kwami-lk-api"}


@router.get("/")
async def root():
    """Root endpoint with API info.

    The version is read from ``src.__version__`` rather than written here. This
    literal said "0.1.0" while the package said "0.1.1": `scripts/set-version.sh`
    rewrites `src/__init__.py`, `pyproject.toml`, `uv.lock` and the README badge
    during a release, and never touched this string, so it drifted one release
    further behind every time. `src/__init__.py` calls itself "the one place the
    running service states its version"; this is what makes that true.

    `docs` is null when the UIs are closed, rather than advertising a 404.
    """
    return {
        "name": "Kwami AI LiveKit API",
        "version": __version__,
        "docs": "/docs" if settings.show_docs else None,
    }
