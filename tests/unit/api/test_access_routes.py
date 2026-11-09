"""`/access`: status codes, and that one user never reaches another's list."""

from __future__ import annotations

import uuid

import pytest

from src.services import site_access_service as s

pytestmark = pytest.mark.anyio


def _rows(fake_supabase):
    return fake_supabase.db.rows(s.TABLE)


async def test_every_route_needs_a_login(client):
    rid = uuid.uuid4()
    assert (await client.get("/access")).status_code == 401
    assert (await client.get("/access/check", params={"address": "a.com"})).status_code == 401
    assert (await client.put("/access/default", json={"access": "read"})).status_code == 401
    body = {"address": "a.com", "access": "read"}
    assert (await client.put("/access/sites", json=body)).status_code == 401
    assert (await client.delete(f"/access/sites/{rid}")).status_code == 401
    assert (await client.post(f"/access/requests/{rid}", json={"approve": True})).status_code == 401


async def test_a_site_round_trip(tenant_client, fake_supabase):
    put = await tenant_client.put(
        "/access/sites", json={"address": "https://www.github.com/x", "access": "write"}
    )
    assert put.status_code == 200 and put.json()["pattern"] == "github.com"

    assert (await tenant_client.put("/access/default", json={"access": "read"})).json() == {
        "default": "read"
    }
    body = (await tenant_client.get("/access")).json()
    assert body["default"] == "read" and body["sites"][0]["access"] == "write"

    check = await tenant_client.get("/access/check", params={"address": "api.github.com"})
    assert check.json() == {"host": "api.github.com", "access": "write", "pattern": "github.com"}

    gone = await tenant_client.delete(f"/access/sites/{put.json()['id']}")
    assert gone.status_code == 204
    again = await tenant_client.delete(f"/access/sites/{put.json()['id']}")
    assert again.status_code == 404


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"address": "localhost", "access": "read"}, 400),
        ({"address": "github.com", "access": "root"}, 400),
    ],
)
async def test_a_bad_site_is_a_400(tenant_client, fake_supabase, body, status):
    assert (await tenant_client.put("/access/sites", json=body)).status_code == status
    assert _rows(fake_supabase) == []


async def test_a_full_list_is_a_409(tenant_client, fake_supabase, monkeypatch):
    monkeypatch.setattr(s, "MAX_RULES", 0)
    response = await tenant_client.put("/access/sites", json={"address": "a.com", "access": "read"})
    assert response.status_code == 409


async def test_a_bad_default_or_check_is_a_400(tenant_client, fake_supabase):
    assert (await tenant_client.put("/access/default", json={"access": "x"})).status_code == 400
    bad = await tenant_client.get("/access/check", params={"address": "localhost"})
    assert bad.status_code == 400


async def test_answering_a_request(tenant_client, tenant, fake_supabase):
    asked = fake_supabase.db.seed(
        s.TABLE,
        {
            "user_id": tenant.user_id,
            "pattern": "amazon.es",
            "access": "account",
            "status": "pending",
            "reason": "prices",
        },
    )[0]
    path = f"/access/requests/{asked['id']}"

    bad = await tenant_client.post(path, json={"approve": True, "access": "god"})
    assert bad.status_code == 400

    ok = await tenant_client.post(path, json={"approve": True, "access": "read"})
    assert ok.status_code == 200 and ok.json()["site"]["access"] == "read"

    missing = await tenant_client.post(path, json={"approve": True})
    assert missing.status_code == 404
    refused = await tenant_client.post(path, json={"approve": False})
    assert refused.status_code == 200 and refused.json() == {"site": None}


async def test_another_user_cannot_touch_the_list(
    tenant_client, other_tenant_client, fake_supabase
):
    put = await tenant_client.put("/access/sites", json={"address": "a.com", "access": "read"})
    rid = put.json()["id"]
    assert (await other_tenant_client.delete(f"/access/sites/{rid}")).status_code == 404
    assert (await other_tenant_client.get("/access")).json()["sites"] == []
    assert len(_rows(fake_supabase)) == 1
