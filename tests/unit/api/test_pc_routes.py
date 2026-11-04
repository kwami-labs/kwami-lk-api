"""`/pc`: the state of the user's PC and the orders that sign it in."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.services import pc_service
from src.services.credits import get_supabase_admin

pytestmark = pytest.mark.anyio


async def _runner(user_id: str, *, login: bool = True, seen: str | None = None) -> None:
    await (
        get_supabase_admin()
        .table("brain_runners")
        .insert(
            {
                "user_id": user_id,
                "runner_id": "r1",
                "claude_login": login,
                "last_seen_at": seen or datetime.now(UTC).isoformat(),
            }
        )
        .execute()
    )


@pytest.mark.parametrize(
    ("typed", "url"),
    [
        ("github.com", "https://github.com"),
        (" https://mail.google.com/x ", "https://mail.google.com/x"),
        ("http://example.org", "http://example.org"),
    ],
)
def test_addresses_get_their_scheme(typed: str, url: str) -> None:
    assert pc_service.site_address(typed) == url


@pytest.mark.parametrize(
    "typed",
    [
        "localhost",
        "http://localhost:8081",
        "127.0.0.1",
        "http://192.168.1.1",
        "printer.local",
        "app.localhost",
        "file:///etc/passwd",
        "javascript://a.b",
        "nothing",
        "a.b/" + "x" * 500,
    ],
)
def test_what_is_not_a_public_site_is_refused(typed: str) -> None:
    with pytest.raises(ValueError, match="web address|public site"):
        pc_service.site_address(typed)


class TestPcRoutes:
    async def test_without_a_runner_it_is_offline(self, tenant_client):
        r = await tenant_client.get("/pc/status")
        assert r.status_code == 200
        assert r.json() == {"online": False, "claude_login": False}

    async def test_a_runner_long_gone_is_offline(self, tenant_client, tenant):
        await _runner(tenant.user_id, seen="2020-01-01T00:00:00+00:00")
        assert (await tenant_client.get("/pc/status")).json()["online"] is False

    async def test_a_fresh_runner_reports_its_login(self, tenant_client, tenant):
        await _runner(tenant.user_id)
        assert (await tenant_client.get("/pc/status")).json() == {
            "online": True,
            "claude_login": True,
        }

    async def test_an_order_needs_the_pc(self, tenant_client, tenant):
        r = await tenant_client.post("/pc/claude", json={"kwami_id": tenant.kwami_id})
        assert r.status_code == 409
        r = await tenant_client.post(
            "/pc/sites", json={"kwami_id": tenant.kwami_id, "address": "github.com"}
        )
        assert r.status_code == 409

    async def test_only_on_your_own_kwami(self, tenant_client, tenant, other_tenant):
        await _runner(tenant.user_id)
        r = await tenant_client.post("/pc/claude", json={"kwami_id": other_tenant.kwami_id})
        assert r.status_code == 400

    async def test_a_bad_address_is_a_400(self, tenant_client, tenant):
        await _runner(tenant.user_id)
        r = await tenant_client.post(
            "/pc/sites", json={"kwami_id": tenant.kwami_id, "address": "localhost"}
        )
        assert r.status_code == 400

    async def test_a_site_is_queued_and_can_be_followed(self, tenant_client, tenant):
        await _runner(tenant.user_id)
        r = await tenant_client.post(
            "/pc/sites", json={"kwami_id": tenant.kwami_id, "address": "github.com"}
        )
        assert r.status_code == 200
        order_id = r.json()["id"]
        row = (await get_supabase_admin().table("brain_jobs").select("*").execute()).data[0]
        assert row["mode"] == "site" and row["task"] == "https://github.com"
        assert row["watcher_seen_at"] and row["delivered_at"]
        r = await tenant_client.get(f"/pc/orders/{order_id}")
        assert r.status_code == 200

    async def test_claude_is_queued(self, tenant_client, tenant):
        await _runner(tenant.user_id)
        r = await tenant_client.post("/pc/claude", json={"kwami_id": tenant.kwami_id})
        assert r.status_code == 200
        row = (await get_supabase_admin().table("brain_jobs").select("*").execute()).data[0]
        assert row["mode"] == "login"

    async def test_someone_elses_order_is_not_found(self, tenant_client):
        r = await tenant_client.get("/pc/orders/00000000-0000-4000-8000-000000000000")
        assert r.status_code == 404

    async def test_a_site_joins_the_list_and_can_be_forgotten(self, tenant_client, tenant):
        await _runner(tenant.user_id)
        await tenant_client.post(
            "/pc/sites", json={"kwami_id": tenant.kwami_id, "address": "GitHub.com/settings"}
        )
        sites = (await tenant_client.get("/pc/sites")).json()["sites"]
        assert [(s["host"], s["state"]) for s in sites] == [("github.com", "pending")]
        r = await tenant_client.delete("/pc/sites/github.com")
        assert r.status_code == 204
        assert (await tenant_client.get("/pc/sites")).json()["sites"] == []
