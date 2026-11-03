"""`/assistant` and `/google`: status codes, ownership, and what never reaches a client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from src.services import google_service, google_workspace

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
        r = await tenant_client.post(
            "/assistant/settings/notify-code",
            json={"kwami_id": other_tenant.kwami_id, "notify_to": "612345678"},
        )
        assert r.status_code in (400, 404)
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
        assert r.json() == {"configured": True, "connected": False, "email": None, "accounts": []}

    async def test_connect(self, tenant_client, google_on):
        r = await tenant_client.post("/google/connect", json={"desktop": True})
        assert r.json()["url"].startswith(google_service.AUTH_URL)

    async def test_connect_from_the_app_comes_back_to_it(
        self, tenant_client, tenant, google_on, monkeypatch
    ):
        monkeypatch.setattr(google_service.settings, "cors_origins_str", "https://kwami.test")
        r = await tenant_client.post("/google/connect", headers={"Origin": "https://kwami.test"})
        state = httpx.URL(r.json()["url"]).params["state"]
        assert google_service.read_state(state).target == "https://kwami.test/"

    async def test_connect_from_an_unknown_origin_is_a_400(self, tenant_client, google_on):
        r = await tenant_client.post("/google/connect", headers={"Origin": "https://evil.test"})
        assert r.status_code == 400

    async def test_connect_when_not_configured(self, tenant_client, google_off):
        r = await tenant_client.post("/google/connect", json={"desktop": True})
        assert r.status_code == 503

    def _state(self, user_id: str) -> str:
        url = google_service.authorization_url(user_id, "kwami://back")
        return httpx.URL(url).params["state"]

    def _complete(self, monkeypatch) -> dict:
        seen: dict = {}

        async def complete(user_id, code):
            seen.update(user=user_id, code=code)
            return "yo@gmail.com"

        monkeypatch.setattr(google_service, "complete_connection", complete)
        return seen

    async def test_the_callback_connects_nothing(self, client, tenant, google_on, monkeypatch):
        """It hands the code to the app: whoever opened the link may not be who started it."""
        seen = self._complete(monkeypatch)
        state = self._state(tenant.user_id)
        r = await client.get(
            "/google/callback", params={"state": state, "code": "c"}, follow_redirects=False
        )
        assert r.status_code == 302
        back = httpx.URL(r.headers["location"])
        assert str(back).startswith("kwami://back#")
        assert dict(httpx.QueryParams(back.fragment)) == {
            "connect": "google",
            "state": state,
            "grant": "c",
        }
        assert seen == {}

    async def test_the_app_finishes_it(self, tenant_client, tenant, google_on, monkeypatch):
        seen = self._complete(monkeypatch)
        r = await tenant_client.post(
            "/google/complete", json={"state": self._state(tenant.user_id), "code": "c"}
        )
        assert r.json() == {"email": "yo@gmail.com"}
        assert seen == {"user": tenant.user_id, "code": "c"}

    async def test_someone_else_cannot_finish_it(self, tenant_client, google_on, monkeypatch):
        """The link sent to a stranger: their code reaches their app, and stops there."""
        seen = self._complete(monkeypatch)
        r = await tenant_client.post(
            "/google/complete", json={"state": self._state("someone-else"), "code": "c"}
        )
        assert r.status_code == 403
        assert seen == {}

    async def test_finishing_needs_a_signed_in_user(self, client, tenant, google_on):
        r = await client.post(
            "/google/complete", json={"state": self._state(tenant.user_id), "code": "c"}
        )
        assert r.status_code == 401

    async def test_finishing_with_a_forged_state_is_a_400(self, tenant_client, google_on):
        r = await tenant_client.post("/google/complete", json={"state": "forged", "code": "c"})
        assert r.status_code == 400

    async def test_finishing_when_not_configured_is_a_503(self, tenant_client, google_off):
        r = await tenant_client.post("/google/complete", json={"state": "s", "code": "c"})
        assert r.status_code == 503

    async def test_finishing_when_google_refuses_is_a_400(
        self, tenant_client, tenant, google_on, monkeypatch
    ):
        async def refused(_user_id, _code):
            raise google_service.GoogleNotConnectedError("no")

        monkeypatch.setattr(google_service, "complete_connection", refused)
        r = await tenant_client.post(
            "/google/complete", json={"state": self._state(tenant.user_id), "code": "c"}
        )
        assert r.status_code == 400

    async def test_the_work_token_opens_only_its_user(
        self, tenant_client, client, tenant, google_on, monkeypatch
    ):
        from src.services import google_worker

        async def run(user_id, **_kwargs):
            return {"user": user_id}

        monkeypatch.setitem(google_worker.ALLOWED, "accounts", run)
        made = await tenant_client.post("/google/work-thread/token")
        assert made.json()["expires_in"] == google_service.WORK_TOKEN_TTL_SECONDS
        r = await client.post(
            "/google/work-thread",
            json={"action": "accounts"},
            headers={"X-Kwami-Work-Token": made.json()["token"]},
        )
        assert r.json() == {"result": {"user": tenant.user_id}}

    async def test_making_a_work_token_when_not_configured(self, tenant_client, google_off):
        assert (await tenant_client.post("/google/work-thread/token")).status_code == 503

    async def test_making_a_work_token_needs_a_signed_in_user(self, client, google_on):
        assert (await client.post("/google/work-thread/token")).status_code == 401

    @pytest.mark.parametrize("headers", [{}, {"X-Kwami-Work-Token": "forged"}])
    async def test_the_work_thread_needs_a_real_token(self, client, google_on, headers):
        r = await client.post("/google/work-thread", json={"action": "accounts"}, headers=headers)
        assert r.status_code == 401

    async def test_the_work_thread_when_not_configured(self, client, google_off):
        r = await client.post(
            "/google/work-thread", json={"action": "accounts"}, headers={"X-Kwami-Work-Token": "t"}
        )
        assert r.status_code == 503

    async def test_sending_is_refused_to_the_work_thread(self, client, tenant, google_on):
        r = await client.post(
            "/google/work-thread",
            json={"action": "gmail_send"},
            headers={"X-Kwami-Work-Token": google_service.work_thread_token(tenant.user_id)},
        )
        assert r.status_code == 400

    @pytest.mark.parametrize(
        "params", [{"error": "access_denied"}, {"state": "forged", "code": "c"}, {}]
    )
    async def test_callback_failures_are_a_plain_page(self, client, google_on, params):
        r = await client.get("/google/callback", params=params)
        assert r.status_code == 400
        assert "No se ha conectado" in r.text

    async def test_disconnect(self, tenant_client, google_on, monkeypatch):
        assert (await tenant_client.delete("/google/account")).status_code == 404

        async def done(_user, _account=None):
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
        async def fail(*_args, **_kwargs):
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
            "create_draft",
            "list_drafts",
            "send_draft",
            "read_attachment",
            "list_labels",
            "create_label",
            "modify_labels",
            "trash_message",
            "untrash_message",
            "list_filters",
            "create_filter",
            "delete_filter",
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
            ("POST", "/google/gmail/drafts", {"to": ["a@b.c"], "body": "x"}, None),
            ("GET", "/google/gmail/drafts", None, None),
            ("POST", "/google/gmail/drafts/d1/send", None, None),
            ("GET", "/google/gmail/messages/m1/attachments/a1", None, None),
            ("GET", "/google/gmail/labels", None, None),
            ("POST", "/google/gmail/labels", {"name": "n"}, None),
            ("POST", "/google/gmail/messages/m1/labels", {"add": ["L"]}, None),
            ("POST", "/google/gmail/messages/m1/trash", None, None),
            ("POST", "/google/gmail/messages/m1/untrash", None, None),
            ("GET", "/google/gmail/filters", None, None),
            ("POST", "/google/gmail/filters", {"criteria": {}, "action": {}}, None),
            ("DELETE", "/google/gmail/filters/f1", None, None),
        ]
        for method, url, body, params in calls:
            r = await tenant_client.request(method, url, json=body, params=params)
            assert r.status_code == 200, url

    async def test_every_workspace_route_reaches_its_service(self, tenant_client, monkeypatch):
        async def answer(*_args, **_kwargs):
            return {"id": "x"}

        async def count(*_args, **_kwargs):
            return 1

        async def nothing(*_args, **_kwargs):
            return None

        async def text(*_args, **_kwargs):
            return "x"

        for name, fake in (
            ("search_files", answer),
            ("read_file", answer),
            ("create_file", answer),
            ("write_file", answer),
            ("create_folder", answer),
            ("update_file", answer),
            ("copy_file", answer),
            ("set_trashed", answer),
            ("list_permissions", answer),
            ("share_file", answer),
            ("unshare_file", nothing),
            ("create_document", answer),
            ("append_to_document", nothing),
            ("replace_in_document", count),
            ("create_spreadsheet", answer),
            ("read_range", answer),
            ("write_range", count),
            ("append_rows", nothing),
            ("create_presentation", answer),
            ("add_slide", text),
            ("search_contacts", answer),
            ("create_contact", answer),
            ("update_contact", answer),
            ("list_task_lists", answer),
            ("list_tasks", answer),
            ("create_task", answer),
            ("update_task", answer),
        ):
            monkeypatch.setattr(google_workspace, name, fake)
        cells = {"range": "A1", "values": [[1]]}
        calls = [
            ("GET", "/google/drive/files", None, {"q": "x"}),
            ("GET", "/google/drive/files/f1", None, None),
            ("POST", "/google/drive/files", {"name": "n"}, None),
            ("PUT", "/google/drive/files/f1/content", {"content": "c"}, None),
            ("POST", "/google/drive/folders", {"name": "n"}, None),
            ("PATCH", "/google/drive/files/f1", {"name": "n"}, None),
            ("POST", "/google/drive/files/f1/copy", {}, None),
            ("POST", "/google/drive/files/f1/trash", None, None),
            ("POST", "/google/drive/files/f1/untrash", None, None),
            ("GET", "/google/drive/files/f1/permissions", None, None),
            ("POST", "/google/drive/files/f1/permissions", {"email": "a@b.c"}, None),
            ("DELETE", "/google/drive/files/f1/permissions/p1", None, None),
            ("POST", "/google/docs", {"title": "t"}, None),
            ("POST", "/google/docs/d1/append", {"text": "t"}, None),
            ("POST", "/google/docs/d1/replace", {"find": "a", "replace": "b"}, None),
            ("POST", "/google/sheets", {"title": "t"}, None),
            ("GET", "/google/sheets/s1/values", None, {"range": "A1"}),
            ("PUT", "/google/sheets/s1/values", cells, None),
            ("POST", "/google/sheets/s1/values", cells, None),
            ("POST", "/google/slides", {"title": "t"}, None),
            ("POST", "/google/slides/p1/slides", {"title": "t"}, None),
            ("GET", "/google/contacts", None, None),
            ("POST", "/google/contacts", {"name": "n"}, None),
            ("PATCH", "/google/contacts/people/c1", {"name": "n"}, None),
            ("GET", "/google/tasks/lists", None, None),
            ("GET", "/google/tasks", None, None),
            ("POST", "/google/tasks", {"title": "t"}, None),
            ("PATCH", "/google/tasks/t1", {"done": True}, None),
        ]
        for method, url, body, params in calls:
            r = await tenant_client.request(method, url, json=body, params=params)
            assert r.status_code == 200, url

    async def test_a_message_takes_its_attachments_from_drive(self, tenant_client, monkeypatch):
        seen = {}

        async def attach(_user, ids, **_kwargs):
            return [google_service.Attachment(ids[0], "text/plain", b"x")]

        async def send(*_args, **kwargs):
            seen.update(kwargs)
            return {"id": "m"}

        monkeypatch.setattr(google_workspace, "drive_attachments", attach)
        monkeypatch.setattr(google_service, "send_message", send)
        r = await tenant_client.post(
            "/google/gmail/send",
            json={"to": ["a@b.c"], "body": "x", "attachment_file_ids": ["f1"]},
        )
        assert r.status_code == 200
        assert seen["attachments"][0].filename == "f1"

    async def test_saving_an_attachment(self, tenant_client, monkeypatch):
        async def save(*_args, **_kwargs):
            return {"id": "f"}

        monkeypatch.setattr(google_workspace, "save_attachment", save)
        r = await tenant_client.post(
            "/google/gmail/messages/m/attachments/a/save", json={"filename": "a.pdf"}
        )
        assert r.json() == {"file": {"id": "f"}}

    async def test_a_route_passes_the_account_it_was_given(self, tenant_client, monkeypatch):
        seen = {}

        async def answer(*_args, **kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr(google_workspace, "search_files", answer)
        await tenant_client.get("/google/drive/files", params={"account": "a@b.c"})
        assert seen["account"] == "a@b.c"


class TestNotifyCode:
    async def test_the_proof_comes_back_and_confirms_the_number(
        self, tenant_client, tenant, monkeypatch
    ):
        codes = []

        async def send(_kind, _user, _kwami, _to, text):
            codes.append(text.split(" es ")[1][:6])

        monkeypatch.setattr("src.services.assistant_scheduler._send_over", send)
        body = {"kwami_id": tenant.kwami_id, "notify_to": "612345678", "notify_channel": "sms"}
        r = await tenant_client.post("/assistant/settings/notify-code", json=body)
        assert r.status_code == 200

        r = await tenant_client.put(
            "/assistant/settings",
            json={**body, "notify_code": codes[0], "notify_proof": r.json()["proof"]},
        )
        assert r.status_code == 200
        assert r.json()["settings"]["notify_to"] == "+34612345678"

    async def test_saving_a_new_number_without_it_is_refused(self, tenant_client, tenant):
        r = await tenant_client.put(
            "/assistant/settings", json={"kwami_id": tenant.kwami_id, "notify_to": "612345678"}
        )
        assert r.status_code == 400

    async def test_a_code_that_cannot_be_sent_says_so(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/assistant/settings/notify-code",
            json={"kwami_id": tenant.kwami_id, "notify_to": "612345678"},
        )
        assert r.status_code == 502
