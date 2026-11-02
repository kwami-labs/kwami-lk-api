"""`src.services.google_service`: the link, the token at rest, Gmail and Calendar."""

from __future__ import annotations

import base64
import json
from email import message_from_bytes

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from src.services import google_service as g

pytestmark = pytest.mark.anyio

KEY = Fernet.generate_key().decode()
USER = "user-1"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(g.settings, "google_oauth_client_id", "cid", raising=False)
    monkeypatch.setattr(g.settings, "google_oauth_client_secret", "sec", raising=False)
    monkeypatch.setattr(g.settings, "google_token_key", KEY, raising=False)
    monkeypatch.setattr(g.settings, "app_public_url", "https://api.test/", raising=False)
    g._access_tokens.clear()


@pytest.fixture
def linked(fake_supabase):
    fake_supabase.db.seed(
        "kwami_google_accounts",
        {
            "user_id": USER,
            "email": "yo@gmail.com",
            "refresh_token_encrypted": Fernet(KEY.encode()).encrypt(b"refresh").decode(),
        },
    )
    g._access_tokens[USER] = ("access", float("inf"))
    return fake_supabase


class TestLink:
    def test_the_consent_url_asks_for_offline_access(self):
        url = g.authorization_url(USER)
        query = httpx.URL(url).params
        assert query["access_type"] == "offline"
        assert query["redirect_uri"] == "https://api.test/google/callback"
        assert "gmail.modify" in query["scope"]
        assert g.read_state(query["state"]) == USER

    def test_no_client_means_not_configured(self, monkeypatch):
        monkeypatch.setattr(g.settings, "google_oauth_client_id", None, raising=False)
        with pytest.raises(g.GoogleNotConfiguredError):
            g.authorization_url(USER)

    def test_a_forged_state_is_refused(self):
        with pytest.raises(ValueError, match="state"):
            g.read_state("forged")

    @respx.mock
    async def test_completing_stores_the_token_encrypted(self, fake_supabase):
        respx.post(g.TOKEN_URL).respond(
            json={"refresh_token": "secret-refresh", "access_token": "a", "scope": "s"}
        )
        respx.get(g.USERINFO_URL).respond(json={"email": "yo@gmail.com"})
        assert await g.complete_connection(USER, "code") == "yo@gmail.com"
        row = fake_supabase.db.rows("kwami_google_accounts")[0]
        assert "secret-refresh" not in row["refresh_token_encrypted"]
        assert (
            Fernet(KEY.encode()).decrypt(row["refresh_token_encrypted"].encode())
            == b"secret-refresh"
        )

    @respx.mock
    async def test_no_refresh_token_is_an_error(self, fake_supabase):
        respx.post(g.TOKEN_URL).respond(json={"access_token": "a"})
        with pytest.raises(g.GoogleNotConnectedError, match="refresh"):
            await g.complete_connection(USER, "code")

    @respx.mock
    async def test_a_refused_code_is_an_error(self, fake_supabase):
        respx.post(g.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        with pytest.raises(g.GoogleNotConnectedError):
            await g.complete_connection(USER, "code")

    @respx.mock
    async def test_an_unknown_identity_is_an_error(self, fake_supabase):
        respx.post(g.TOKEN_URL).respond(json={"refresh_token": "r", "access_token": "a"})
        respx.get(g.USERINFO_URL).respond(401)
        with pytest.raises(g.GoogleNotConnectedError, match="whose"):
            await g.complete_connection(USER, "code")

    async def test_status(self, fake_supabase):
        assert await g.connection_status(USER) == {
            "configured": True,
            "connected": False,
            "email": None,
        }

    async def test_status_when_linked_never_shows_the_token(self, linked):
        status = await g.connection_status(USER)
        assert status["email"] == "yo@gmail.com"
        assert "refresh_token_encrypted" not in status

    @respx.mock
    async def test_disconnect_revokes_and_forgets(self, linked):
        revoke = respx.post(g.REVOKE_URL).respond(200)
        assert await g.disconnect(USER) is True
        assert revoke.called
        assert not linked.db.rows("kwami_google_accounts")
        assert USER not in g._access_tokens

    @respx.mock
    async def test_disconnect_forgets_even_if_google_fails(self, linked):
        respx.post(g.REVOKE_URL).mock(side_effect=httpx.ConnectError("down"))
        assert await g.disconnect(USER) is True
        assert not linked.db.rows("kwami_google_accounts")

    async def test_disconnect_with_nothing_linked(self, fake_supabase):
        assert await g.disconnect(USER) is False


class TestAccessToken:
    async def test_no_account(self, fake_supabase):
        with pytest.raises(g.GoogleNotConnectedError, match="linked"):
            await g._access_token(USER)

    @respx.mock
    async def test_it_refreshes_then_caches(self, linked):
        g._access_tokens.clear()
        route = respx.post(g.TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
        assert await g._access_token(USER) == "new"
        assert await g._access_token(USER) == "new"
        assert route.call_count == 1

    async def test_an_unreadable_token_is_not_connected(self, fake_supabase):
        fake_supabase.db.seed(
            "kwami_google_accounts",
            {"user_id": USER, "email": "e", "refresh_token_encrypted": "garbage"},
        )
        with pytest.raises(g.GoogleNotConnectedError, match="read"):
            await g._access_token(USER)


class TestErrors:
    @respx.mock
    async def test_a_401_drops_the_cached_token(self, linked):
        respx.get(f"{g.GMAIL_URL}/messages").respond(401)
        with pytest.raises(g.GoogleNotConnectedError):
            await g.list_messages(USER)
        assert USER not in g._access_tokens

    @respx.mock
    async def test_other_errors_carry_no_upstream_text(self, linked):
        respx.get(f"{g.GMAIL_URL}/messages").respond(403, json={"error": "secret detail"})
        with pytest.raises(g.GoogleRequestError) as caught:
            await g.list_messages(USER)
        assert "secret" not in str(caught.value)


class TestGmail:
    @respx.mock
    async def test_listing(self, linked):
        listing = respx.get(f"{g.GMAIL_URL}/messages").respond(json={"messages": [{"id": "m1"}]})
        respx.get(f"{g.GMAIL_URL}/messages/m1").respond(
            json={
                "id": "m1",
                "threadId": "t1",
                "snippet": "hola",
                "labelIds": ["UNREAD", "INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "a@b.c"},
                        {"name": "Subject", "value": "Asunto"},
                    ]
                },
            }
        )
        messages = await g.list_messages(USER, "is:unread", 99)
        assert listing.calls[0].request.url.params["maxResults"] == str(g.MAX_LIST_RESULTS)
        assert messages[0]["from"] == "a@b.c"
        assert messages[0]["subject"] == "Asunto"
        assert messages[0]["unread"] is True

    @respx.mock
    async def test_an_empty_inbox(self, linked):
        respx.get(f"{g.GMAIL_URL}/messages").respond(json={})
        assert await g.list_messages(USER) == []

    @respx.mock
    async def test_reading_finds_the_plain_text_in_nested_parts(self, linked):
        respx.get(f"{g.GMAIL_URL}/messages/m1").respond(
            json={
                "id": "m1",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": _b64("<b>no</b>")}},
                        {
                            "mimeType": "multipart/alternative",
                            "parts": [{"mimeType": "text/plain", "body": {"data": _b64("sí ñ")}}],
                        },
                    ],
                },
            }
        )
        assert (await g.read_message(USER, "m1"))["body"] == "sí ñ"

    @respx.mock
    async def test_html_only_has_no_body(self, linked):
        respx.get(f"{g.GMAIL_URL}/messages/m1").respond(
            json={
                "id": "m1",
                "payload": {"mimeType": "text/html", "body": {"data": _b64("<b>x</b>")}},
            }
        )
        assert (await g.read_message(USER, "m1"))["body"] == ""

    @respx.mock
    async def test_sending(self, linked):
        route = respx.post(f"{g.GMAIL_URL}/messages/send").respond(
            json={"id": "s1", "threadId": "t9"}
        )
        sent = await g.send_message(USER, to=["a@b.c"], cc=["d@e.f"], subject="Hola", body="Texto")
        assert sent == {"id": "s1", "thread_id": "t9"}
        raw = json.loads(route.calls[0].request.content)["raw"]
        mail = message_from_bytes(base64.urlsafe_b64decode(raw))
        assert mail["To"] == "a@b.c"
        assert mail["Cc"] == "d@e.f"
        assert mail["Subject"] == "Hola"

    @respx.mock
    async def test_replying_stays_in_the_thread(self, linked):
        respx.get(f"{g.GMAIL_URL}/messages/m1").respond(
            json={
                "threadId": "t1",
                "payload": {
                    "headers": [
                        {"name": "Message-ID", "value": "<abc@x>"},
                        {"name": "Subject", "value": "Cita"},
                    ]
                },
            }
        )
        route = respx.post(f"{g.GMAIL_URL}/messages/send").respond(json={"id": "s1"})
        await g.send_message(USER, to=["a@b.c"], subject="", body="Ok", reply_to_message_id="m1")
        sent = json.loads(route.calls[0].request.content)
        assert sent["threadId"] == "t1"
        mail = message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
        assert mail["Subject"] == "Re: Cita"
        assert mail["In-Reply-To"] == "<abc@x>"

    @respx.mock
    async def test_a_reply_keeps_an_existing_re_and_a_given_subject(self, linked):
        respx.get(f"{g.GMAIL_URL}/messages/m1").respond(
            json={"payload": {"headers": [{"name": "Subject", "value": "RE: Cita"}]}}
        )
        route = respx.post(f"{g.GMAIL_URL}/messages/send").respond(json={"id": "s1"})
        await g.send_message(USER, to=["a@b.c"], subject="", body="Ok", reply_to_message_id="m1")
        await g.send_message(
            USER, to=["a@b.c"], subject="Otro", body="Ok", reply_to_message_id="m1"
        )
        subjects = [
            message_from_bytes(base64.urlsafe_b64decode(json.loads(call.request.content)["raw"]))[
                "Subject"
            ]
            for call in route.calls
        ]
        assert subjects == ["RE: Cita", "Otro"]

    async def test_a_line_break_in_a_header_is_refused(self, linked):
        with pytest.raises(ValueError):
            await g.send_message(USER, to=["a@b.c"], subject="x\nBcc: spy@x.y", body="Ok")

    @respx.mock
    async def test_archiving_removes_the_inbox_label(self, linked):
        route = respx.post(f"{g.GMAIL_URL}/messages/m1/modify").respond(json={})
        await g.archive_message(USER, "m1")
        assert json.loads(route.calls[0].request.content) == {"removeLabelIds": ["INBOX"]}


class TestCalendar:
    @respx.mock
    async def test_listing_handles_timed_and_all_day(self, linked):
        respx.get(f"{g.CALENDAR_URL}/events").respond(
            json={
                "items": [
                    {
                        "id": "e1",
                        "summary": "Reunión",
                        "start": {"dateTime": "2026-11-03T10:00:00Z"},
                        "end": {"dateTime": "2026-11-03T11:00:00Z"},
                        "attendees": [{"email": "a@b.c"}],
                    },
                    {"id": "e2", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}},
                ]
            }
        )
        events = await g.list_events(USER, "2026-11-01T00:00:00Z", "2026-11-08T00:00:00Z")
        assert events[0]["title"] == "Reunión"
        assert events[0]["attendees"] == ["a@b.c"]
        assert events[0]["all_day"] is False
        assert events[1]["starts_at"] == "2026-11-04"
        assert events[1]["all_day"] is True

    @respx.mock
    async def test_creating_with_attendees_notifies_them(self, linked):
        route = respx.post(f"{g.CALENDAR_URL}/events").respond(json={"id": "e1", "summary": "R"})
        await g.create_event(
            USER,
            title="R",
            starts_at="2026-11-03T10:00:00Z",
            ends_at="2026-11-03T11:00:00Z",
            description="d",
            location="l",
            attendees=["a@b.c"],
        )
        request = route.calls[0].request
        assert request.url.params["sendUpdates"] == "all"
        assert json.loads(request.content) == {
            "summary": "R",
            "description": "d",
            "location": "l",
            "start": {"dateTime": "2026-11-03T10:00:00Z"},
            "end": {"dateTime": "2026-11-03T11:00:00Z"},
            "attendees": [{"email": "a@b.c"}],
        }

    @respx.mock
    async def test_updating_sends_only_what_changed_and_notifies_nobody(self, linked):
        route = respx.patch(f"{g.CALENDAR_URL}/events/e1").respond(json={"id": "e1"})
        await g.update_event(USER, "e1", title="Nuevo", starts_at=None, attendees=None)
        request = route.calls[0].request
        assert request.url.params["sendUpdates"] == "none"
        assert json.loads(request.content) == {"summary": "Nuevo"}

    @respx.mock
    async def test_deleting(self, linked):
        route = respx.delete(f"{g.CALENDAR_URL}/events/e1").respond(204)
        await g.delete_event(USER, "e1")
        assert route.called

    @respx.mock
    async def test_updating_only_the_description_leaves_the_title_alone(self, linked):
        route = respx.patch(f"{g.CALENDAR_URL}/events/e1").respond(json={"id": "e1"})
        await g.update_event(USER, "e1", description="d")
        assert json.loads(route.calls[0].request.content) == {"description": "d"}


class TestIdsInThePath:
    @pytest.mark.parametrize("bad", ["../../other", "a/b", "a?x=1", "a%2Fb", "", "x" * 1025])
    async def test_what_is_not_a_google_id_never_goes_out(self, linked, bad):
        with respx.mock(assert_all_called=False) as router:
            route = router.route()
            for call in (
                g.read_message(USER, bad),
                g.archive_message(USER, bad),
                g.update_event(USER, bad, title="x"),
                g.delete_event(USER, bad),
            ):
                with pytest.raises(g.GoogleRequestError):
                    await call
            assert not route.called

    @respx.mock
    async def test_a_recurring_event_id_is_one(self, linked):
        event = "abc123_20261102T100000Z"
        route = respx.delete(f"{g.CALENDAR_URL}/events/{event}").mock(
            return_value=httpx.Response(204)
        )
        await g.delete_event(USER, event)
        assert route.called

    @respx.mock
    async def test_the_log_does_not_carry_the_address(self, linked, caplog):
        respx.get(f"{g.GMAIL_URL}/messages/abc").mock(return_value=httpx.Response(500))
        with caplog.at_level("WARNING", logger="kwami-api"), pytest.raises(g.GoogleRequestError):
            await g.read_message(USER, "abc")
        assert "Google answered 500" in caplog.text and "messages/abc" not in caplog.text
