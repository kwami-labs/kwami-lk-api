"""`/assistant` and `/google`: status codes, ownership, and what never reaches a client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from src.services import google_service

pytestmark = pytest.mark.anyio

KEY = Fernet.generate_key().decode()


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(google_service.settings, "google_oauth_client_id", "c", raising=False)
    monkeypatch.setattr(google_service.settings, "google_oauth_client_secret", "s", raising=False)
    monkeypatch.setattr(google_service.settings, "google_token_key", KEY, raising=False)
    monkeypatch.setattr(google_service.settings, "app_public_url", "https://a.b", raising=False)


@pytest.fixture
def google_off(monkeypatch):
    monkeypatch.setattr(google_service.settings, "google_oauth_client_id", None, raising=False)


class TestAssistantSettings:
    async def test_reading_and_saving(self, tenant_client, tenant):
        r = await tenant_client.get("/assistant/settings", params={"kwami_id": tenant.kwami_id})
        assert r.status_code == 200
        assert r.json()["settings"]["reports_enabled"] is False
        r = await tenant_client.put(
            "/assistant/settings", json={"kwami_id": tenant.kwami_id, "reports_enabled": True}
        )
        assert r.status_code == 200
        assert r.json()["settings"]["reports_enabled"] is True

    async def test_another_kwami_is_a_400(self, tenant_client, other_tenant):
        r = await tenant_client.get(
            "/assistant/settings", params={"kwami_id": other_tenant.kwami_id}
        )
        assert r.status_code == 400
        r = await tenant_client.put("/assistant/settings", json={"kwami_id": other_tenant.kwami_id})
        assert r.status_code == 400


class TestAssistantTasks:
    async def test_the_whole_life_of_a_task(self, tenant_client, tenant):
        run_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        r = await tenant_client.post(
            "/assistant/tasks",
            json={"kwami_id": tenant.kwami_id, "title": "Llamar", "run_at": run_at},
        )
        assert r.status_code == 200
        task_id = r.json()["task"]["id"]
        r = await tenant_client.get("/assistant/tasks", params={"kwami_id": tenant.kwami_id})
        assert [t["id"] for t in r.json()["tasks"]] == [task_id]
        r = await tenant_client.patch(f"/assistant/tasks/{task_id}", json={"status": "done"})
        assert r.json()["task"]["status"] == "done"
        assert (await tenant_client.delete(f"/assistant/tasks/{task_id}")).json() == {"ok": True}
        assert (await tenant_client.delete(f"/assistant/tasks/{task_id}")).status_code == 404

    async def test_refusals(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/assistant/tasks", json={"kwami_id": tenant.kwami_id, "title": "Sin hora"}
        )
        assert r.status_code == 400
        r = await tenant_client.get(
            "/assistant/tasks", params={"kwami_id": tenant.kwami_id, "status": "x"}
        )
        assert r.status_code == 400
        assert (await tenant_client.patch("/assistant/tasks/none", json={})).status_code == 404
        r = await tenant_client.patch("/assistant/tasks/none", json={"status": "x"})
        assert r.status_code == 400


class TestNotificationsAndAudit:
    async def test_notifications(self, tenant_client, tenant, fake_supabase):
        row = fake_supabase.db.seed(
            "kwami_assistant_notifications",
            {
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "title": "T",
                "body": "B",
                "read_at": None,
            },
        )[0]
        r = await tenant_client.get(
            "/assistant/notifications", params={"kwami_id": tenant.kwami_id, "unread_only": True}
        )
        assert len(r.json()["notifications"]) == 1
        r = await tenant_client.post(f"/assistant/notifications/{row['id']}/read")
        assert r.json() == {"ok": True}
        r = await tenant_client.post("/assistant/notifications/none/read")
        assert r.status_code == 404

    async def test_notifications_of_another_kwami(self, tenant_client, other_tenant):
        r = await tenant_client.get(
            "/assistant/notifications", params={"kwami_id": other_tenant.kwami_id}
        )
        assert r.status_code == 400

    async def test_the_send_record(self, tenant_client, tenant, other_tenant):
        body = {
            "kwami_id": tenant.kwami_id,
            "channel": "gmail",
            "recipient": "a@b.c",
            "express_order": "sin revisar",
        }
        r = await tenant_client.post("/assistant/express-sends", json=body)
        assert r.json()["record"]["express_order"] == "sin revisar"
        r = await tenant_client.post(
            "/assistant/express-sends", json={**body, "kwami_id": other_tenant.kwami_id}
        )
        assert r.status_code == 400


class TestGoogleLink:
    async def test_it_needs_a_session(self, client):
        assert (await client.get("/google/status")).status_code == 401

    async def test_status(self, tenant_client, google_on):
        r = await tenant_client.get("/google/status")
        assert r.json() == {"configured": True, "connected": False, "email": None}

    async def test_connect(self, tenant_client, google_on):
        r = await tenant_client.post("/google/connect")
        assert r.json()["url"].startswith(google_service.AUTH_URL)

    async def test_connect_when_not_configured(self, tenant_client, google_off):
        assert (await tenant_client.post("/google/connect")).status_code == 503

    async def test_callback_links_the_user_in_the_state(
        self, client, tenant, google_on, monkeypatch
    ):
        seen = {}

        async def complete(user_id, code):
            seen.update(user=user_id, code=code)
            return "yo@gmail.com"

        monkeypatch.setattr(google_service, "complete_connection", complete)
        state = Fernet(KEY.encode()).encrypt(tenant.user_id.encode()).decode()
        r = await client.get("/google/callback", params={"state": state, "code": "c"})
        assert r.status_code == 200
        assert "Cuenta conectada" in r.text
        assert seen == {"user": tenant.user_id, "code": "c"}

    @pytest.mark.parametrize(
        "params", [{"error": "access_denied"}, {"state": "forged", "code": "c"}, {}]
    )
    async def test_callback_failures_are_a_plain_page(self, client, google_on, params):
        r = await client.get("/google/callback", params=params)
        assert r.status_code == 400
        assert "No se ha conectado" in r.text

    async def test_disconnect(self, tenant_client, google_on, monkeypatch):
        assert (await tenant_client.delete("/google/account")).status_code == 404

        async def done(_user):
            return True

        monkeypatch.setattr(google_service, "disconnect", done)
        assert (await tenant_client.delete("/google/account")).json() == {"ok": True}


class TestGoogleData:
    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (google_service.GoogleNotConfiguredError("x"), 503),
            (google_service.GoogleNotConnectedError("x"), 409),
            (google_service.GoogleRequestError("x"), 502),
            (ValueError("bad header"), 400),
        ],
    )
    async def test_errors_map_to_status_codes(self, tenant_client, monkeypatch, error, status):
        async def fail(*_args):
            raise error

        monkeypatch.setattr(google_service, "list_messages", fail)
        assert (await tenant_client.get("/google/gmail/messages")).status_code == status

    async def test_every_route_reaches_its_service(self, tenant_client, monkeypatch):
        async def answer(*_args, **_kwargs):
            return {"id": "x"}

        for name in (
            "list_messages",
            "read_message",
            "send_message",
            "archive_message",
            "list_events",
            "create_event",
            "update_event",
            "delete_event",
        ):
            monkeypatch.setattr(google_service, name, answer)
        event = {"title": "R", "starts_at": "a", "ends_at": "b"}
        calls = [
            ("GET", "/google/gmail/messages", None, None),
            ("GET", "/google/gmail/messages/m1", None, None),
            ("POST", "/google/gmail/send", {"to": ["a@b.c"], "body": "x"}, None),
            ("POST", "/google/gmail/messages/m1/archive", None, None),
            ("GET", "/google/calendar/events", None, {"range_start": "a", "range_end": "b"}),
            ("POST", "/google/calendar/events", event, None),
            ("PATCH", "/google/calendar/events/e1", {"title": "N"}, None),
            ("DELETE", "/google/calendar/events/e1", None, None),
        ]
        for method, url, body, params in calls:
            r = await tenant_client.request(method, url, json=body, params=params)
            assert r.status_code == 200, url
