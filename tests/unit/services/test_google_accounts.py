"""Several linked accounts, a link that dies on its own, and the new Gmail calls."""

from __future__ import annotations

import base64
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from src.services import google_service as g

pytestmark = pytest.mark.anyio

KEY = Fernet.generate_key().decode()
USER = "user-1"
FIRST = "yo@gmail.com"
SECOND = "Claude@nexow.ai"


def _token(text: str) -> str:
    return Fernet(KEY.encode()).encrypt(text.encode()).decode()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(g.settings, "google_oauth_client_id", "cid", raising=False)
    monkeypatch.setattr(g.settings, "google_oauth_client_secret", "sec", raising=False)
    monkeypatch.setattr(g.settings, "google_token_key", KEY, raising=False)
    monkeypatch.setattr(g.settings, "app_public_url", "https://api.test/", raising=False)
    g._access_tokens.clear()


@pytest.fixture
def two_linked(fake_supabase):
    fake_supabase.db.seed(
        "kwami_google_accounts",
        {
            "user_id": USER,
            "email": FIRST,
            "refresh_token_encrypted": _token("refresh-1"),
            "scopes": " ".join(g.SCOPES),
            "created_at": "2026-11-01T00:00:00+00:00",
        },
        {
            "user_id": USER,
            "email": SECOND,
            "refresh_token_encrypted": _token("refresh-2"),
            "scopes": "openid",
            "created_at": "2026-11-02T00:00:00+00:00",
        },
    )
    return fake_supabase


def _json(route) -> dict:
    return json.loads(route.calls.last.request.content)


class TestSeveralAccounts:
    def test_the_consent_screen_lets_the_user_choose_the_account(self):
        query = httpx.URL(g.authorization_url(USER)).params
        assert query["prompt"] == "select_account consent"
        assert query["access_type"] == "offline"

    def test_google_can_return_to_a_local_api(self, monkeypatch):
        assert g.redirect_uri() == "https://api.test/google/callback"
        monkeypatch.setattr(
            g.settings, "google_redirect_base_url", "http://localhost:8080/", raising=False
        )
        assert g.redirect_uri() == "http://localhost:8080/google/callback"

    def test_it_never_asks_for_the_scope_that_deletes_mail_for_good(self):
        assert "https://mail.google.com/" not in g.SCOPES

    @respx.mock
    async def test_a_second_account_does_not_replace_the_first(self, two_linked):
        respx.post(g.TOKEN_URL).respond(json={"refresh_token": "r3", "access_token": "a"})
        respx.get(g.USERINFO_URL).respond(json={"email": "tercera@gmail.com"})
        await g.complete_connection(USER, "code")
        assert len(two_linked.db.rows("kwami_google_accounts")) == 3

    @respx.mock
    async def test_linking_again_mends_an_account_that_needed_it(self, two_linked):
        two_linked.db.rows("kwami_google_accounts")[0]["needs_reconnect_at"] = "2026-11-02"
        respx.post(g.TOKEN_URL).respond(json={"refresh_token": "r3", "access_token": "a"})
        respx.get(g.USERINFO_URL).respond(json={"email": FIRST})
        await g.complete_connection(USER, "code")
        rows = two_linked.db.rows("kwami_google_accounts")
        assert len(rows) == 2
        assert rows[0]["needs_reconnect_at"] is None

    @respx.mock
    async def test_an_answer_with_no_address_is_an_error(self, fake_supabase):
        respx.post(g.TOKEN_URL).respond(json={"refresh_token": "r", "access_token": "a"})
        respx.get(g.USERINFO_URL).respond(json={})
        with pytest.raises(g.GoogleNotConnectedError, match="whose"):
            await g.complete_connection(USER, "code")

    async def test_the_name_google_gives_email_is_not_a_missing_permission(self, monkeypatch):
        granted = [s for s in g.SCOPES if s != "email"]
        granted.append("https://www.googleapis.com/auth/userinfo.email")

        async def accounts(_user):
            return [{"email": FIRST, "scopes": " ".join(granted)}]

        monkeypatch.setattr(g, "_accounts", accounts)
        status = await g.connection_status(USER)
        assert status["accounts"][0]["missing_scopes"] == []

    async def test_status_lists_each_one_and_what_it_still_lacks(self, two_linked):
        status = await g.connection_status(USER)
        assert status["email"] == FIRST
        first, second = status["accounts"]
        assert first == {"email": FIRST, "needs_reconnect": False, "missing_scopes": []}
        assert second["email"] == SECOND
        assert "https://www.googleapis.com/auth/drive" in second["missing_scopes"]
        assert "refresh" not in json.dumps(status)

    @respx.mock
    async def test_each_address_uses_its_own_token(self, two_linked):
        route = respx.post(g.TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
        await g._access_token(USER, "claude@NEXOW.ai")
        assert "refresh-2" in route.calls.last.request.content.decode()
        await g._access_token(USER)
        assert "refresh-1" in route.calls.last.request.content.decode()

    async def test_an_address_that_is_not_linked_is_not_connected(self, two_linked):
        with pytest.raises(g.GoogleNotConnectedError):
            await g._access_token(USER, "nadie@gmail.com")

    @respx.mock
    async def test_disconnecting_one_keeps_the_other(self, two_linked):
        respx.post(g.REVOKE_URL).respond(200)
        assert await g.disconnect(USER, SECOND) is True
        rows = two_linked.db.rows("kwami_google_accounts")
        assert [row["email"] for row in rows] == [FIRST]


class TestALinkThatDies:
    @respx.mock
    async def test_a_refused_refresh_is_recorded_for_the_app(self, two_linked):
        respx.post(g.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        with pytest.raises(g.GoogleNotConnectedError):
            await g._access_token(USER, FIRST)
        status = await g.connection_status(USER)
        assert status["accounts"][0]["needs_reconnect"] is True
        assert status["accounts"][1]["needs_reconnect"] is False


class TestCalls:
    @respx.mock
    async def test_a_document_body_is_cut(self):
        g._access_tokens[(USER, "")] = ("a", float("inf"))
        respx.get("https://x.test/doc").respond(text="x" * (g.MAX_BODY_CHARS + 5))
        assert len(await g.google_text(USER, "https://x.test/doc")) == g.MAX_BODY_CHARS

    @respx.mock
    async def test_a_content_type_can_be_set(self):
        g._access_tokens[(USER, "")] = ("a", float("inf"))
        route = respx.post("https://x.test/up").respond(json={"ok": 1})
        await g.google_json(
            USER, "POST", "https://x.test/up", content=b"x", content_type="text/plain"
        )
        assert route.calls.last.request.headers["content-type"] == "text/plain"


class TestGmail:
    @pytest.fixture(autouse=True)
    def token(self):
        g._access_tokens[(USER, "")] = ("access", float("inf"))

    @respx.mock
    async def test_a_message_lists_its_attachments(self):
        respx.get(f"{g.GMAIL_URL}/messages/m1").respond(
            json={
                "id": "m1",
                "payload": {
                    "parts": [
                        {
                            "filename": "a.pdf",
                            "mimeType": "application/pdf",
                            "body": {"attachmentId": "at1", "size": 9},
                        },
                        {"filename": "", "mimeType": "text/plain", "body": {"data": ""}},
                    ]
                },
            }
        )
        message = await g.read_message(USER, "m1")
        assert message["attachments"] == [
            {"id": "at1", "filename": "a.pdf", "mime_type": "application/pdf", "size": 9}
        ]

    @respx.mock
    async def test_an_attachment_gives_text_or_only_its_size(self):
        url = f"{g.GMAIL_URL}/messages/m1/attachments/at1"
        text = base64.urlsafe_b64encode(b"hola").decode()
        respx.get(url).respond(json={"data": text})
        assert await g.read_attachment(USER, "m1", "at1") == {"size": 4, "text": "hola"}
        binary = base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode()
        respx.get(url).respond(json={"data": binary})
        assert await g.read_attachment(USER, "m1", "at1") == {"size": 3, "text": None}

    @respx.mock
    async def test_a_draft_is_not_a_sent_message(self):
        draft = respx.post(f"{g.GMAIL_URL}/drafts").respond(json={"id": "d1"})
        sent = respx.post(f"{g.GMAIL_URL}/messages/send").respond(json={"id": "m1"})
        assert await g.create_draft(USER, to=["a@b.c"], subject="s", body="b") == {"id": "d1"}
        assert draft.called
        assert "raw" in _json(draft)["message"]
        assert not sent.called

    @respx.mock
    async def test_a_message_can_carry_attachments(self):
        route = respx.post(f"{g.GMAIL_URL}/messages/send").respond(json={"id": "m1"})
        await g.send_message(
            USER,
            to=["a@b.c"],
            subject="s",
            body="b",
            attachments=[g.Attachment("a.pdf", "application/pdf", b"%PDF")],
        )
        raw = base64.urlsafe_b64decode(_json(route)["raw"])
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        found = next(parsed.iter_attachments())
        assert found.get_filename() == "a.pdf"
        assert found.get_content() == b"%PDF"

    @respx.mock
    async def test_listing_drafts_keeps_the_draft_id(self):
        respx.get(f"{g.GMAIL_URL}/drafts").respond(json={"drafts": [{"id": "d1"}]})
        respx.get(f"{g.GMAIL_URL}/drafts/d1").respond(json={"message": {"id": "m1"}})
        assert (await g.list_drafts(USER))[0]["id"] == "d1"

    @respx.mock
    async def test_sending_a_draft(self):
        route = respx.post(f"{g.GMAIL_URL}/drafts/send").respond(json={"id": "m1", "threadId": "t"})
        assert await g.send_draft(USER, "d1") == {"id": "m1", "thread_id": "t"}
        assert _json(route) == {"id": "d1"}

    @respx.mock
    async def test_labels(self):
        respx.get(f"{g.GMAIL_URL}/labels").respond(
            json={"labels": [{"id": "L1", "name": "n", "type": "user"}]}
        )
        assert await g.list_labels(USER) == [{"id": "L1", "name": "n", "type": "user"}]
        respx.post(f"{g.GMAIL_URL}/labels").respond(json={"id": "L2", "name": "m"})
        assert await g.create_label(USER, "m") == {"id": "L2", "name": "m"}
        route = respx.post(f"{g.GMAIL_URL}/messages/m1/modify").respond(json={})
        await g.modify_labels(USER, "m1", add=["L1"])
        assert _json(route) == {"addLabelIds": ["L1"], "removeLabelIds": []}

    @respx.mock
    async def test_the_bin_goes_both_ways_and_nothing_deletes_for_good(self):
        trash = respx.post(f"{g.GMAIL_URL}/messages/m1/trash").respond(json={})
        back = respx.post(f"{g.GMAIL_URL}/messages/m1/untrash").respond(json={})
        await g.trash_message(USER, "m1")
        await g.untrash_message(USER, "m1")
        assert trash.called
        assert back.called
        assert not hasattr(g, "delete_message")

    @respx.mock
    async def test_filters(self):
        respx.get(f"{g.GMAIL_URL}/settings/filters").respond(json={"filter": [{"id": "f1"}]})
        assert await g.list_filters(USER) == [{"id": "f1"}]
        route = respx.post(f"{g.GMAIL_URL}/settings/filters").respond(json={"id": "f2"})
        made = await g.create_filter(
            USER, criteria={"from": "a@b.c"}, action={"addLabelIds": ["L1"]}
        )
        assert made == {"id": "f2"}
        assert _json(route)["criteria"] == {"from": "a@b.c"}
        gone = respx.delete(f"{g.GMAIL_URL}/settings/filters/f1").respond(204)
        await g.delete_filter(USER, "f1")
        assert gone.called

    async def test_a_filter_cannot_forward_mail(self):
        with pytest.raises(ValueError, match="forward"):
            await g.create_filter(USER, criteria={}, action={"forward": "x@evil.test"})
