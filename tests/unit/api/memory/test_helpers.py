"""The module-level helpers in `src.api.routes.memory`.

`thread_belongs_to` and `iter_all_threads` are covered in
tests/unit/api/test_memory_tenancy.py; this covers the rest.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.routes import memory as mem_mod
from src.api.routes.memory import (
    DEFAULT_EDGE_TYPES,
    DEFAULT_ENTITY_TYPES,
    _build_ontology_models,
    get_zep_client,
    verify_user_access,
)
from src.core.security import AuthUser


class TestGetZepClient:
    @pytest.mark.anyio
    async def test_an_unconfigured_key_is_a_503(self, monkeypatch):
        monkeypatch.setattr(mem_mod.settings, "zep_api_key", None, raising=False)
        with pytest.raises(HTTPException) as exc:
            await get_zep_client()
        assert exc.value.status_code == 503
        assert "ZEP_API_KEY" in exc.value.detail

    @pytest.mark.anyio
    async def test_a_configured_key_builds_a_client(self, monkeypatch):
        built: list[str] = []
        monkeypatch.setattr(mem_mod.settings, "zep_api_key", "z-key", raising=False)
        monkeypatch.setattr(mem_mod, "AsyncZep", lambda api_key: built.append(api_key) or "CLIENT")
        assert await get_zep_client() == "CLIENT"
        assert built == ["z-key"]


class TestVerifyUserAccess:
    def test_your_own_namespace_is_allowed(self):
        assert verify_user_access(AuthUser({"sub": "u1"}), "u1") is None

    def test_a_per_kwami_namespace_is_allowed(self):
        assert verify_user_access(AuthUser({"sub": "u1"}), "kwami_u1_abc") is None

    def test_another_users_namespace_is_a_403(self):
        with pytest.raises(HTTPException) as exc:
            verify_user_access(AuthUser({"sub": "u1"}), "u2")
        assert exc.value.status_code == 403
        assert "only access your own memory" in exc.value.detail

    def test_a_substring_namespace_is_a_403(self):
        """The anchored rules in `check_user_access` are what this leans on."""
        with pytest.raises(HTTPException):
            verify_user_access(AuthUser({"sub": "u1"}), "kwami_u12_abc")


class TestBuildOntologyModels:
    def test_the_shipped_defaults_build(self):
        entities, edges = _build_ontology_models(DEFAULT_ENTITY_TYPES, DEFAULT_EDGE_TYPES)
        assert set(entities) == {e["name"] for e in DEFAULT_ENTITY_TYPES}
        assert set(edges) == {e["name"] for e in DEFAULT_EDGE_TYPES}

    def test_an_entity_becomes_a_model_class_carrying_its_description(self):
        entities, _ = _build_ontology_models(
            [{"name": "Preference", "description": "What the user likes."}], []
        )
        model = entities["Preference"]
        assert model.__name__ == "Preference"
        assert model.__doc__ == "What the user likes."

    def test_an_edge_carries_a_source_target_pair(self):
        _, edges = _build_ontology_models([], [{"name": "KNOWS", "description": "d"}])
        model_cls, targets = edges["KNOWS"]
        assert model_cls.__name__ == "KNOWS"
        assert targets[0].source == "User"

    def test_a_missing_description_falls_back_to_the_name(self):
        entities, edges = _build_ontology_models([{"name": "Thing"}], [{"name": "LINKS"}])
        assert entities["Thing"].__doc__ == "Thing"
        assert edges["LINKS"][0].__doc__ == "LINKS"

    def test_empty_input_yields_empty_models(self):
        assert _build_ontology_models([], []) == ({}, {})

    def test_the_default_sets_are_non_trivial(self):
        """A truncated default ontology would quietly degrade every new user."""
        assert len(DEFAULT_ENTITY_TYPES) >= 10
        assert len(DEFAULT_EDGE_TYPES) >= 10
        assert all("description" in e for e in DEFAULT_ENTITY_TYPES)
        assert all("description" in e for e in DEFAULT_EDGE_TYPES)


class TestSharedZepClient:
    """One client per process, closed at shutdown.

    A fresh `AsyncZep` per request meant a fresh httpx connection pool per
    request across all 29 memory routes, none of them ever closed.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        mem_mod._zep_client = None
        yield
        mem_mod._zep_client = None

    @pytest.mark.anyio
    async def test_an_unconfigured_key_is_a_503(self, monkeypatch):
        monkeypatch.setattr(mem_mod.settings, "zep_api_key", None, raising=False)
        with pytest.raises(HTTPException) as excinfo:
            await mem_mod.get_zep_client()
        assert excinfo.value.status_code == 503

    @pytest.mark.anyio
    async def test_the_client_is_built_once_and_reused(self, monkeypatch):
        monkeypatch.setattr(mem_mod.settings, "zep_api_key", "zep-key", raising=False)
        built: list[str] = []

        class _Zep:
            def __init__(self, api_key):
                built.append(api_key)

        monkeypatch.setattr(mem_mod, "AsyncZep", _Zep)
        first = await mem_mod.get_zep_client()
        second = await mem_mod.get_zep_client()
        assert first is second
        assert built == ["zep-key"], "one client, not one per request"

    @pytest.mark.anyio
    async def test_closing_without_a_client_is_a_no_op(self):
        await mem_mod.close_zep_client()  # must not raise

    @pytest.mark.anyio
    async def test_closing_releases_the_connection_pool(self, monkeypatch):
        monkeypatch.setattr(mem_mod.settings, "zep_api_key", "zep-key", raising=False)
        closed: list[bool] = []

        class _Httpx:
            async def aclose(self):
                closed.append(True)

        class _Zep:
            def __init__(self, api_key):
                self.httpx_client = _Httpx()

        monkeypatch.setattr(mem_mod, "AsyncZep", _Zep)
        await mem_mod.get_zep_client()
        await mem_mod.close_zep_client()
        assert closed == [True]
        assert mem_mod._zep_client is None

    @pytest.mark.anyio
    async def test_a_transport_on_the_wrapper_is_also_closed(self, monkeypatch):
        """zep-cloud has moved the transport between versions."""
        monkeypatch.setattr(mem_mod.settings, "zep_api_key", "zep-key", raising=False)
        closed: list[bool] = []

        class _Httpx:
            async def aclose(self):
                closed.append(True)

        class _Zep:
            def __init__(self, api_key):
                self._client_wrapper = type("W", (), {"httpx_client": _Httpx()})()

        monkeypatch.setattr(mem_mod, "AsyncZep", _Zep)
        await mem_mod.get_zep_client()
        await mem_mod.close_zep_client()
        assert closed == [True]

    @pytest.mark.anyio
    async def test_a_client_with_no_reachable_transport_is_dropped_quietly(self, monkeypatch):
        monkeypatch.setattr(mem_mod.settings, "zep_api_key", "zep-key", raising=False)
        monkeypatch.setattr(mem_mod, "AsyncZep", lambda api_key: object())
        await mem_mod.get_zep_client()
        await mem_mod.close_zep_client()
        assert mem_mod._zep_client is None
