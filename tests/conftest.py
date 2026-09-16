"""Shared test configuration and fixtures.

Two things here are load-bearing and easy to undo by accident:

1. ``KWAMI_ENV_FILE`` is pointed at ``tests/.env.test`` *before* anything from
   ``src`` is imported. ``Settings`` reads ``.env`` by default, and the real
   ``.env`` in the repo root holds live provider credentials -- without this a
   test that reaches an un-mocked path talks to production.
2. The real SDKs are used. A previous version of this file replaced ``livekit``,
   ``supabase``, ``stripe`` and ``zep_cloud`` with hand-built stubs in
   ``sys.modules``; that broke collection outright the first time a new SDK import
   appeared, and it made signature-verification tests vacuous because the stub
   ``construct_event`` ignored the signature. External calls are intercepted at
   the client seam or at the HTTP layer instead.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import pytest

os.environ["KWAMI_ENV_FILE"] = str(Path(__file__).parent / ".env.test")

from fastapi import FastAPI, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from src.api.deps import get_current_user  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.security import AuthUser  # noqa: E402
from src.main import app as _app  # noqa: E402
from src.services import credits  # noqa: E402
from tests.factories.tenant import Tenant, make_tenant_factory  # noqa: E402
from tests.fakes.supabase import FakeSupabase  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def app_instance() -> FastAPI:
    """The application under test.

    Everything else depends on this fixture, so swapping the module-level
    singleton for ``create_app(settings)`` is a change in one place.
    """
    return _app


@pytest.fixture(autouse=True)
def isolate_dependency_overrides(app_instance: FastAPI):
    """Snapshot and restore ``dependency_overrides`` around every test.

    Fixtures used to end with ``app.dependency_overrides.clear()``, which wiped
    overrides installed by *sibling* fixtures (``test_memory.py``'s
    ``get_zep_client`` was the casualty). Because the app is a process-wide
    singleton that leaked in both directions across the session.
    """
    saved = dict(app_instance.dependency_overrides)
    yield
    app_instance.dependency_overrides.clear()
    app_instance.dependency_overrides.update(saved)


@pytest.fixture(autouse=True)
def fake_supabase(monkeypatch) -> FakeSupabase:
    """Swap the database for every test.

    ``src.services.credits.get_supabase_admin`` is the single seam: all 66 call
    sites across channels / email / calendar / wallet / reconciliation reach the
    database through it, so setting the cached client here covers the whole app
    and ``create_client`` is never called.
    """
    fake = FakeSupabase()
    monkeypatch.setattr(credits, "_supabase_client", fake, raising=False)
    return fake


@pytest.fixture
def tenant_factory(fake_supabase: FakeSupabase) -> Callable[..., Tenant]:
    """Seed a user + kwami (+ optional channel / email account / wallet).

    Signature is shared with the integration harness, so an api-layer test
    promotes to a real-database test by swapping the backing fixture.
    """
    return make_tenant_factory(fake_supabase.db)


@pytest.fixture
def tenant(tenant_factory: Callable[..., Tenant]) -> Tenant:
    return tenant_factory(email="owner@example.com")


@pytest.fixture
def other_tenant(tenant_factory: Callable[..., Tenant]) -> Tenant:
    """A second tenant, for asserting cross-tenant access is denied."""
    return tenant_factory(email="intruder@example.com")


@pytest.fixture
def mock_auth_user() -> AuthUser:
    return AuthUser(
        {
            "sub": "test-user-id",
            "email": "test@example.com",
            "role": "authenticated",
            "aud": "authenticated",
        }
    )


TEST_USER_HEADER = "X-Test-User"


@pytest.fixture
def auth_registry(app_instance: FastAPI) -> dict[str, AuthUser]:
    """Per-request test identity.

    Overriding ``get_current_user`` with ``lambda: some_user`` breaks as soon as a
    test needs two identities: the app is a singleton, both client fixtures write
    the same key, and whichever is constructed last silently wins -- so a
    cross-tenant test can make *both* requests as the attacker and pass for the
    wrong reason.

    The override is installed once and resolves the caller from a header each
    client sets, which is genuinely per-request.
    """
    registry: dict[str, AuthUser] = {}

    def resolve(request: Request) -> AuthUser | None:
        return registry.get(request.headers.get(TEST_USER_HEADER, ""))

    app_instance.dependency_overrides[get_current_user] = resolve
    return registry


def _asgi_client(app_instance: FastAPI, user: AuthUser | None = None) -> AsyncClient:
    headers = {TEST_USER_HEADER: user.id} if user else {}
    return AsyncClient(
        transport=ASGITransport(app=app_instance),
        base_url="http://test",
        headers=headers,
    )


@pytest.fixture
async def client(app_instance: FastAPI, auth_registry) -> AsyncGenerator[AsyncClient, None]:
    """Unauthenticated client."""
    async with _asgi_client(app_instance) as c:
        yield c


@pytest.fixture
async def auth_client(
    app_instance: FastAPI,
    mock_auth_user: AuthUser,
    auth_registry: dict[str, AuthUser],
    fake_supabase: FakeSupabase,
) -> AsyncGenerator[AsyncClient, None]:
    """Client authenticated as ``mock_auth_user`` (``test-user-id``), in credit.

    The credits row is seeded here rather than in ``fake_supabase`` so the
    database starts empty for tests that assert on their own seeded state.
    ``/token`` refuses to issue below a zero balance, so an authenticated client
    without credits could not reach any of the behaviour these tests cover.
    """
    fake_supabase.db.seed(
        "user_credits",
        {
            "user_id": mock_auth_user.id,
            "balance": 500_000,
            "lifetime_purchased": 500_000,
            "lifetime_used": 0,
        },
    )
    auth_registry[mock_auth_user.id] = mock_auth_user
    async with _asgi_client(app_instance, mock_auth_user) as c:
        yield c


@pytest.fixture
async def tenant_client(
    app_instance: FastAPI, tenant: Tenant, auth_registry: dict[str, AuthUser]
) -> AsyncGenerator[AsyncClient, None]:
    """Client authenticated as the seeded ``tenant`` (owns a kwami)."""
    auth_registry[tenant.user_id] = tenant.auth_user
    async with _asgi_client(app_instance, tenant.auth_user) as c:
        yield c


@pytest.fixture
async def other_tenant_client(
    app_instance: FastAPI, other_tenant: Tenant, auth_registry: dict[str, AuthUser]
) -> AsyncGenerator[AsyncClient, None]:
    """Client authenticated as a *different* tenant, for authorization tests."""
    auth_registry[other_tenant.user_id] = other_tenant.auth_user
    async with _asgi_client(app_instance, other_tenant.auth_user) as c:
        yield c


@pytest.fixture
async def admin_client(app_instance: FastAPI, auth_registry) -> AsyncGenerator[AsyncClient, None]:
    """Client carrying a valid ``X-Admin-API-Key``."""
    async with _asgi_client(app_instance) as c:
        c.headers["X-Admin-API-Key"] = settings.admin_api_key or ""
        yield c


@pytest.fixture
async def internal_client(
    app_instance: FastAPI, auth_registry
) -> AsyncGenerator[AsyncClient, None]:
    """Client carrying the shared agent key, for ``/internal/*`` and usage reports."""
    async with _asgi_client(app_instance) as c:
        c.headers["X-Kwami-API-Key"] = settings.kwami_api_key or ""
        c.headers["X-API-Key"] = settings.kwami_api_key or ""
        yield c
