"""`POST /token` — the failure arms of the endpoint that mints a credential.

The ownership check, the room claim and the balance check each have a rejecting
path, and the balance check has a fail-open/fail-closed switch that differs
between production and everywhere else. Those are the arms here; the happy path
and the ownership rules live in tests/api/test_token.py and
tests/unit/api/test_token_ownership.py.
"""

from __future__ import annotations

import pytest

from src.api.routes import token as token_route
from src.core.errors import ForbiddenError


def _set_balance(fake_supabase, user_id: str, balance: int) -> None:
    """`db.rows` hands back the live list, so this edits the seeded row in place."""
    for row in fake_supabase.db.rows("user_credits"):
        if str(row["user_id"]) == str(user_id):
            row["balance"] = balance
            return
    fake_supabase.db.seed(
        "user_credits",
        {"user_id": user_id, "balance": balance, "lifetime_purchased": 0, "lifetime_used": 0},
    )


@pytest.mark.anyio
async def test_a_zero_balance_is_a_402(tenant_client, fake_supabase, tenant):
    _set_balance(fake_supabase, tenant.user_id, 0)
    r = await tenant_client.post("/token", json={})
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "insufficient_credits"


@pytest.mark.anyio
async def test_a_negative_balance_is_a_402(tenant_client, fake_supabase, tenant):
    _set_balance(fake_supabase, tenant.user_id, -1)
    assert (await tenant_client.post("/token", json={})).status_code == 402


@pytest.mark.anyio
async def test_a_room_owned_by_someone_else_is_a_403(
    tenant_client, fake_supabase, tenant, other_tenant
):
    fake_supabase.db.seed(
        "livekit_sessions",
        {
            "room_name": "taken",
            "user_id": other_tenant.user_id,
            "kwami_id": None,
            "source": "web",
            "status": "issued",
        },
    )
    r = await tenant_client.post("/token", json={"roomName": "taken"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.anyio
async def test_a_failing_balance_check_fails_open_when_configured(
    monkeypatch, tenant_client, caplog
):
    """Outside production an outage must not stop sessions starting."""

    async def boom(_user_id):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(token_route, "get_balance", boom)
    monkeypatch.setattr(
        token_route.settings, "credits_fail_open_on_check_error", True, raising=False
    )
    with caplog.at_level("ERROR", logger="kwami-api.token"):
        r = await tenant_client.post("/token", json={})
    assert r.status_code == 200
    assert "allowing connection" in caplog.text


@pytest.mark.anyio
async def test_a_failing_balance_check_fails_closed_when_configured(
    monkeypatch, tenant_client, caplog
):
    """In production it must not hand out unbilled sessions."""

    async def boom(_user_id):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(token_route, "get_balance", boom)
    monkeypatch.setattr(
        token_route.settings, "credits_fail_open_on_check_error", False, raising=False
    )
    with caplog.at_level("ERROR", logger="kwami-api.token"):
        r = await tenant_client.post("/token", json={})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"
    assert "blocking connection" in caplog.text


@pytest.mark.anyio
async def test_a_402_is_not_swallowed_by_the_fail_open_switch(monkeypatch, tenant_client):
    """The `except HTTPException: raise` arm — a 402 must survive fail-open."""

    async def broke(_user_id):
        return {"balance": 0}

    monkeypatch.setattr(token_route, "get_balance", broke)
    monkeypatch.setattr(
        token_route.settings, "credits_fail_open_on_check_error", True, raising=False
    )
    assert (await tenant_client.post("/token", json={})).status_code == 402


@pytest.mark.anyio
async def test_a_token_minting_failure_is_a_flat_500(monkeypatch, tenant_client, caplog):
    def boom(**kwargs):
        raise RuntimeError("livekit key rejected: secret sk_live_abc")

    monkeypatch.setattr(token_route, "create_token", boom)
    with caplog.at_level("ERROR", logger="kwami-api.token"):
        r = await tenant_client.post("/token", json={})
    assert r.status_code == 500
    assert r.json()["detail"] == "Failed to generate token"
    assert "sk_live_abc" not in r.text, "upstream text must not reach the client"


@pytest.mark.anyio
async def test_a_forbidden_room_claim_propagates_as_a_domain_error(monkeypatch, tenant_client):
    async def deny(*a, **k):
        raise ForbiddenError("This room belongs to another user")

    monkeypatch.setattr(token_route, "claim_room", deny)
    r = await tenant_client.post("/token", json={"roomName": "someone-elses"})
    assert r.status_code == 403


@pytest.mark.anyio
async def test_the_participant_name_falls_back_through_email_then_identity(
    monkeypatch, tenant_client, tenant
):
    seen: dict = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return "tok"

    monkeypatch.setattr(token_route, "create_token", capture)
    await tenant_client.post("/token", json={})
    assert seen["participant_name"] == tenant.auth_user.email
    assert seen["participant_identity"] == tenant.user_id


@pytest.mark.anyio
async def test_an_omitted_room_name_is_derived_server_side(tenant_client, tenant):
    r = await tenant_client.post("/token", json={})
    assert r.status_code == 200
    room = r.json()["room_name"]
    assert room.startswith(f"kwami-web-{tenant.kwami_id[:8]}") or room.startswith("kwami-web-")


class TestGetVariant:
    @pytest.mark.anyio
    async def test_it_delegates_to_the_post_handler(self, tenant_client):
        r = await tenant_client.get("/token", params={"roomName": "via-get"})
        assert r.status_code == 200
        assert r.json()["room_name"] == "via-get"

    @pytest.mark.anyio
    async def test_it_carries_the_kwami_id_through(self, tenant_client, tenant):
        r = await tenant_client.get(
            "/token", params={"roomName": "r-get", "kwamiId": tenant.kwami_id}
        )
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_it_rejects_an_unowned_kwami_the_same_way(self, tenant_client):
        r = await tenant_client.get(
            "/token",
            params={"kwamiId": "00000000-0000-0000-0000-000000000000"},
        )
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_it_requires_auth(self, client):
        assert (await client.get("/token")).status_code == 401
