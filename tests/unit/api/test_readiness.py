"""`/health/ready` — can this instance actually serve traffic?

`/health` returns a static dict, so an instance that could not reach Supabase
still reported healthy and kept receiving requests. Liveness and readiness
answer different questions and must not be the same endpoint: a dependency
outage that fails liveness restarts every instance in a loop, which is worse
than the outage.
"""

from __future__ import annotations

import anyio
import pytest

from src.api.routes import health as health_mod

pytestmark = pytest.mark.anyio

READY = "/health/ready"


@pytest.fixture
def configured(monkeypatch):
    """A deployment with both optional dependencies switched on."""
    monkeypatch.setattr(health_mod.settings, "supabase_url", "https://p.supabase.co", raising=False)
    monkeypatch.setattr(health_mod.settings, "supabase_secret_key", "k", raising=False)
    monkeypatch.setattr(health_mod.settings, "zep_api_key", "z", raising=False)


async def _ok():
    return None


async def _fail():
    raise RuntimeError("upstream down")


async def _hang():
    await anyio.sleep(60)


class TestLiveness:
    async def test_it_checks_nothing_external(self, client, monkeypatch):
        """A dependency outage must not make every instance look dead."""

        async def explode():
            raise AssertionError("liveness must not touch a dependency")

        monkeypatch.setattr(health_mod, "_check_supabase", explode)
        monkeypatch.setattr(health_mod, "_check_zep", explode)
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


class TestReadiness:
    async def test_all_dependencies_up_is_ready(self, client, monkeypatch, configured):
        monkeypatch.setattr(health_mod, "_check_supabase", _ok)
        monkeypatch.setattr(health_mod, "_check_zep", _ok)
        response = await client.get(READY)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["supabase"]["ok"] is True
        assert body["checks"]["zep"]["ok"] is True

    async def test_a_failing_dependency_is_a_503(self, client, monkeypatch, configured):
        """503 is what makes the platform stop routing here."""
        monkeypatch.setattr(health_mod, "_check_supabase", _fail)
        monkeypatch.setattr(health_mod, "_check_zep", _ok)
        response = await client.get(READY)
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["supabase"] == {"ok": False, "error": "RuntimeError"}

    async def test_the_upstream_message_is_not_echoed(self, client, monkeypatch, configured):
        """Only the exception type: the text can carry connection strings."""
        monkeypatch.setattr(health_mod, "_check_supabase", _fail)
        monkeypatch.setattr(health_mod, "_check_zep", _ok)
        assert "upstream down" not in (await client.get(READY)).text

    async def test_a_hanging_dependency_times_out(self, client, monkeypatch, configured):
        """A probe that can hang is worse than none: the platform waits on it."""
        monkeypatch.setattr(health_mod, "DEPENDENCY_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(health_mod, "_check_supabase", _hang)
        monkeypatch.setattr(health_mod, "_check_zep", _ok)
        with anyio.fail_after(5):
            response = await client.get(READY)
        assert response.status_code == 503
        assert response.json()["checks"]["supabase"]["error"] == "timeout"

    async def test_an_unconfigured_dependency_is_not_a_failure(self, client, monkeypatch):
        """A deployment without Zep is a supported deployment, not an unready one."""
        monkeypatch.setattr(health_mod.settings, "supabase_url", None, raising=False)
        monkeypatch.setattr(health_mod.settings, "zep_api_key", None, raising=False)
        response = await client.get(READY)
        assert response.status_code == 200
        assert response.json()["checks"]["zep"]["status"] == "not_configured"
        assert response.json()["checks"]["supabase"]["status"] == "not_configured"

    async def test_the_checks_run_concurrently(self, client, monkeypatch, configured):
        """Two 100ms probes must cost 100ms, not 200ms."""
        delay = 0.1

        async def slow():
            await anyio.sleep(delay)

        monkeypatch.setattr(health_mod, "_check_supabase", slow)
        monkeypatch.setattr(health_mod, "_check_zep", slow)
        import time

        started = time.perf_counter()
        response = await client.get(READY)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200
        assert elapsed < delay * 1.8


class TestRealDependencyProbes:
    """The probe bodies themselves, which the tests above replace."""

    async def test_the_supabase_probe_issues_a_query(self, fake_supabase, monkeypatch):
        await health_mod._check_supabase()  # must not raise against the fake

    async def test_the_zep_probe_lists_a_thread(self, monkeypatch):
        listed: list[dict] = []

        class _Thread:
            async def list_all(self, **kwargs):
                listed.append(kwargs)

        class _Client:
            thread = _Thread()

        async def _client():
            return _Client()

        monkeypatch.setattr(health_mod, "_check_zep", health_mod._check_zep)
        import src.api.routes.memory as memory_mod

        monkeypatch.setattr(memory_mod, "get_zep_client", _client)
        await health_mod._check_zep()
        assert listed == [{"page_number": 1, "page_size": 1}]
