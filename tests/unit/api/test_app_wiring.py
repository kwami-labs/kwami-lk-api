"""`src.main` and `src.api.authz` — how the app is assembled.

The lifespan log line is the only place the running service states its version
and whether the agent shared secret is set; a deployment with `KWAMI_API_KEY`
missing answers 503 to every usage report, and this warning is the only warning
anyone gets.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from src import __version__
from src.api import authz
from src.api.authz import kwami_from_path, kwami_from_query, require_kwami_owned
from src.core.errors import KwamiNotFoundError
from src.main import app, docs_urls, lifespan


class TestLifespan:
    @pytest.mark.anyio
    async def test_it_logs_the_identity_of_the_running_service(self, caplog):
        with caplog.at_level("INFO", logger="kwami-api"):
            async with lifespan(app):
                pass
        assert f"v{__version__}" in caplog.text
        assert "Listening on" in caplog.text
        assert "LiveKit URL" in caplog.text
        assert "Environment" in caplog.text
        assert "Shutting down" in caplog.text

    @pytest.mark.anyio
    async def test_a_configured_agent_key_is_reported_as_set(self, monkeypatch, caplog):
        monkeypatch.setattr(
            lifespan.__wrapped__.__globals__["settings"], "kwami_api_key", "k", raising=False
        )
        with caplog.at_level("INFO", logger="kwami-api"):
            async with lifespan(app):
                pass
        assert "usage report: set" in caplog.text

    @pytest.mark.anyio
    @pytest.mark.parametrize("value", [None, "", "   "])
    async def test_a_missing_or_blank_agent_key_is_warned_about(self, monkeypatch, caplog, value):
        """Blank is the trap: it is truthy-looking in a .env but useless."""
        monkeypatch.setattr(
            lifespan.__wrapped__.__globals__["settings"], "kwami_api_key", value, raising=False
        )
        with caplog.at_level("INFO", logger="kwami-api"):
            async with lifespan(app):
                pass
        assert "NOT SET" in caplog.text
        assert "503" in caplog.text


class TestDocsUrls:
    def test_off_closes_all_three(self):
        assert docs_urls(False) == {"docs_url": None, "redoc_url": None, "openapi_url": None}

    def test_on_opens_all_three(self):
        assert docs_urls(True) == {
            "docs_url": "/docs",
            "redoc_url": "/redoc",
            "openapi_url": "/openapi.json",
        }


def _schema_paths() -> set[str]:
    """The paths as the OpenAPI schema states them — what the app team consumes."""
    return set(app.openapi()["paths"])


class TestRouterWiring:
    def test_every_documented_prefix_is_mounted(self):
        """The README's route table is a contract with the app team."""
        paths = _schema_paths()
        for prefix in (
            "/token",
            "/memory",
            "/models",
            "/voices",
            "/languages",
            "/credits",
            "/channels",
            "/contacts",
            "/wallets",
            "/email",
            "/calendar",
            "/internal",
            "/webhooks",
            "/admin/reconciliation",
        ):
            assert any(p.startswith(prefix) for p in paths), f"{prefix} is not mounted"

    def test_health_is_mounted_at_the_root(self):
        assert {"/", "/health"} <= _schema_paths()

    def test_the_app_reports_the_package_version(self):
        assert app.version == __version__

    def test_cors_is_installed(self):
        from fastapi.middleware.cors import CORSMiddleware

        assert any(m.cls is CORSMiddleware for m in app.user_middleware)

    def test_the_error_handlers_are_installed(self):
        from fastapi.exceptions import RequestValidationError
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from src.core.errors import DomainError

        for exc in (DomainError, StarletteHTTPException, RequestValidationError, Exception):
            assert exc in app.exception_handlers


class TestAuthzDependencies:
    @pytest.mark.anyio
    async def test_the_path_shape_resolves_an_owned_kwami(self, fake_supabase, tenant):
        row = await kwami_from_path(tenant.kwami_id, tenant.auth_user)
        assert row["id"] == tenant.kwami_id

    @pytest.mark.anyio
    async def test_the_path_shape_refuses_someone_elses(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(KwamiNotFoundError):
            await kwami_from_path(tenant.kwami_id, other_tenant.auth_user)

    @pytest.mark.anyio
    async def test_the_query_shape_resolves_an_owned_kwami(self, fake_supabase, tenant):
        row = await kwami_from_query(tenant.auth_user, tenant.kwami_id)
        assert row["id"] == tenant.kwami_id

    @pytest.mark.anyio
    async def test_the_query_shape_refuses_someone_elses(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(KwamiNotFoundError):
            await kwami_from_query(other_tenant.auth_user, tenant.kwami_id)

    def test_the_body_shape_resolves_an_owned_kwami(self, fake_supabase, tenant):
        assert require_kwami_owned(tenant.user_id, tenant.kwami_id)["id"] == tenant.kwami_id

    def test_the_body_shape_refuses_someone_elses(self, fake_supabase, tenant, other_tenant):
        with pytest.raises(KwamiNotFoundError):
            require_kwami_owned(other_tenant.user_id, tenant.kwami_id)

    def test_all_three_shapes_share_one_resolver(self, monkeypatch):
        """Three shapes over one rule -- the point of the module."""
        calls: list[tuple] = []
        monkeypatch.setattr(
            authz, "resolve_owned_kwami", lambda u, k: calls.append((u, k)) or {"id": k}
        )
        require_kwami_owned("u1", "k1")
        assert calls == [("u1", "k1")]

    def test_the_annotated_aliases_are_exported(self):
        from src.api.authz import OwnedKwamiPath, OwnedKwamiQuery

        assert OwnedKwamiPath is not None
        assert OwnedKwamiQuery is not None


def test_a_fresh_app_can_be_built_with_docs_closed():
    """`docs_urls` is spread into the constructor; the kwargs must stay valid."""
    closed = FastAPI(**docs_urls(False))
    assert closed.openapi_url is None
