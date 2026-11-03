"""`google_workspace`: Drive, Docs, Sheets, Slides, Contacts, Tasks, and what is left out."""

from __future__ import annotations

import base64
import inspect
import json

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from src.services import google_service as g
from src.services import google_workspace as w

pytestmark = pytest.mark.anyio

KEY = Fernet.generate_key().decode()
USER = "user-1"
DRIVE_FILE = {"id": "f1", "name": "n", "mimeType": "text/plain"}


@pytest.fixture(autouse=True)
def linked(monkeypatch):
    monkeypatch.setattr(g.settings, "google_token_key", KEY, raising=False)
    g._access_tokens.clear()
    g._access_tokens[(USER, "")] = ("access", float("inf"))
    g._access_tokens[(USER, "otra@gmail.com")] = ("other-access", float("inf"))
    yield
    g._access_tokens.clear()


def _json(route) -> dict:
    return json.loads(route.calls.last.request.content)


class TestDrive:
    @respx.mock
    async def test_a_search_leaves_the_bin_out(self):
        route = respx.get(w.DRIVE_URL).respond(json={"files": [DRIVE_FILE]})
        found = await w.search_files(USER, "name contains 'x'")
        assert found[0]["id"] == "f1"
        assert route.calls.last.request.url.params["q"] == "(name contains 'x') and trashed = false"
        await w.search_files(USER)
        assert route.calls.last.request.url.params["q"] == "trashed = false"

    @respx.mock
    async def test_a_call_uses_the_token_of_the_account_it_names(self):
        route = respx.get(w.DRIVE_URL).respond(json={"files": []})
        await w.search_files(USER, account="otra@gmail.com")
        assert route.calls.last.request.headers["authorization"] == "Bearer other-access"

    @respx.mock
    async def test_a_google_document_is_read_through_export(self):
        respx.get(f"{w.DRIVE_URL}/f1").respond(
            json={**DRIVE_FILE, "mimeType": "application/vnd.google-apps.spreadsheet"}
        )
        export = respx.get(f"{w.DRIVE_URL}/f1/export").respond(text="a,b")
        found = await w.read_file(USER, "f1")
        assert found["text"] == "a,b"
        assert export.calls.last.request.url.params["mimeType"] == "text/csv"

    @respx.mock
    async def test_a_stored_text_file_is_read_as_it_is(self):
        respx.get(f"{w.DRIVE_URL}/f1").mock(
            side_effect=lambda request: (
                httpx.Response(200, text="contenido")
                if request.url.params.get("alt") == "media"
                else httpx.Response(200, json=DRIVE_FILE)
            )
        )
        assert (await w.read_file(USER, "f1"))["text"] == "contenido"

    @respx.mock
    async def test_a_binary_file_has_no_text(self):
        respx.get(f"{w.DRIVE_URL}/f1").respond(json={**DRIVE_FILE, "mimeType": "application/zip"})
        assert (await w.read_file(USER, "f1"))["text"] is None

    @respx.mock
    async def test_a_pdf_is_read_through_a_temporary_copy_that_goes_away(self):
        respx.get(f"{w.DRIVE_URL}/f1").respond(json={**DRIVE_FILE, "mimeType": "application/pdf"})
        copy = respx.post(f"{w.DRIVE_URL}/f1/copy").respond(json={"id": "copy1"})
        respx.get(f"{w.DRIVE_URL}/copy1/export").respond(text="factura")
        gone = respx.delete(f"{w.DRIVE_URL}/copy1").respond(204)
        assert (await w.read_file(USER, "f1"))["text"] == "factura"
        assert _json(copy)["mimeType"] == "application/vnd.google-apps.document"
        assert gone.called

    @respx.mock
    async def test_the_temporary_copy_goes_away_even_if_reading_it_fails(self):
        respx.post(f"{w.DRIVE_URL}/f1/copy").respond(json={"id": "copy1"})
        respx.get(f"{w.DRIVE_URL}/copy1/export").respond(500)
        gone = respx.delete(f"{w.DRIVE_URL}/copy1").respond(204)
        with pytest.raises(g.GoogleRequestError):
            await w._text_by_conversion(USER, "f1", None)
        assert gone.called

    @respx.mock
    async def test_saving_an_attachment_uploads_its_bytes(self):
        respx.get(f"{g.GMAIL_URL}/messages/m1/attachments/a1").respond(
            json={"data": base64.urlsafe_b64encode(b"%PDF-1").decode()}
        )
        upload = respx.post(w.DRIVE_UPLOAD_URL).respond(json=DRIVE_FILE)
        await w.save_attachment(USER, "m1", "a1", filename="f.pdf", mime_type="application/pdf")
        assert b"%PDF-1" in upload.calls.last.request.content
        assert b"f.pdf" in upload.calls.last.request.content

    @respx.mock
    async def test_a_drive_file_becomes_an_attachment_and_a_document_a_pdf(self):
        respx.get(f"{w.DRIVE_URL}/f1").mock(
            side_effect=lambda request: (
                httpx.Response(200, content=b"bytes")
                if request.url.params.get("alt") == "media"
                else httpx.Response(200, json={"name": "a.png", "mimeType": "image/png"})
            )
        )
        respx.get(f"{w.DRIVE_URL}/d1").respond(
            json={"name": "Informe", "mimeType": "application/vnd.google-apps.document"}
        )
        respx.get(f"{w.DRIVE_URL}/d1/export").respond(content=b"%PDF")
        first, second = await w.drive_attachments(USER, ["f1", "d1"])
        assert (first.filename, first.mime_type, first.data) == ("a.png", "image/png", b"bytes")
        assert (second.filename, second.mime_type) == ("Informe.pdf", "application/pdf")

    @respx.mock
    async def test_attachments_over_the_limit_are_refused(self, monkeypatch):
        monkeypatch.setattr(w, "MAX_ATTACHMENT_BYTES", 2)
        respx.get(f"{w.DRIVE_URL}/f1").mock(
            side_effect=lambda request: (
                httpx.Response(200, content=b"bytes")
                if request.url.params.get("alt") == "media"
                else httpx.Response(200, json={})
            )
        )
        with pytest.raises(ValueError, match="25 MB"):
            await w.drive_attachments(USER, ["f1"])

    @respx.mock
    async def test_creating_uploads_metadata_and_content(self):
        route = respx.post(w.DRIVE_UPLOAD_URL).respond(json=DRIVE_FILE)
        await w.create_file(USER, name="n.py", content="print(1)", parent_id="p1")
        request = route.calls.last.request
        assert request.headers["content-type"].startswith("multipart/related; boundary=")
        assert b'"parents": ["p1"]' in request.content
        assert b"print(1)" in request.content
        await w.create_file(USER, name="n.py")
        assert b"parents" not in route.calls.last.request.content

    @respx.mock
    async def test_writing_replaces_the_content(self):
        route = respx.patch(f"{w.DRIVE_UPLOAD_URL}/f1").respond(json=DRIVE_FILE)
        await w.write_file(USER, "f1", content="nuevo")
        assert route.calls.last.request.content == b"nuevo"

    @respx.mock
    async def test_folders(self):
        route = respx.post(w.DRIVE_URL).respond(json=DRIVE_FILE)
        await w.create_folder(USER, name="d", parent_id="p1")
        assert _json(route) == {"name": "d", "mimeType": w.FOLDER_MIME, "parents": ["p1"]}
        await w.create_folder(USER, name="d")
        assert "parents" not in _json(route)

    @respx.mock
    async def test_renaming_and_moving(self):
        route = respx.patch(f"{w.DRIVE_URL}/f1").respond(json=DRIVE_FILE)
        await w.update_file(USER, "f1", name="z", add_parent="a", remove_parent="b")
        params = route.calls.last.request.url.params
        assert (params["addParents"], params["removeParents"]) == ("a", "b")
        assert _json(route) == {"name": "z"}
        await w.update_file(USER, "f1")
        assert _json(route) == {}

    @respx.mock
    async def test_copying(self):
        route = respx.post(f"{w.DRIVE_URL}/f1/copy").respond(json=DRIVE_FILE)
        await w.copy_file(USER, "f1", name="z")
        assert _json(route) == {"name": "z"}
        await w.copy_file(USER, "f1")
        assert _json(route) == {}

    @respx.mock
    async def test_the_bin_only_moves_a_file_there_and_back(self):
        route = respx.patch(f"{w.DRIVE_URL}/f1").respond(json=DRIVE_FILE)
        await w.set_trashed(USER, "f1", True)
        assert _json(route) == {"trashed": True}
        await w.set_trashed(USER, "f1", False)
        assert _json(route) == {"trashed": False}

    def test_nothing_deletes_a_file_or_a_contact_for_good(self):
        names = [name for name in dir(w) if "delete" in name.lower()]
        assert names == []
        # Only the temporary copy goes for good, and the bin is the rest's way out.
        source = inspect.getsource(w)
        assert source.count('"DELETE"') == 2

    @respx.mock
    async def test_sharing_is_with_one_named_person(self):
        route = respx.post(f"{w.DRIVE_URL}/f1/permissions").respond(json={"id": "p"})
        await w.share_file(USER, "f1", email="a@b.c", role="writer")
        assert _json(route) == {"type": "user", "role": "writer", "emailAddress": "a@b.c"}

    @pytest.mark.parametrize("role", ["owner", "organizer", "anyone"])
    async def test_an_owner_role_is_refused(self, role):
        with pytest.raises(ValueError, match="Role"):
            await w.share_file(USER, "f1", email="a@b.c", role=role)

    @respx.mock
    async def test_permissions_and_unsharing(self):
        respx.get(f"{w.DRIVE_URL}/f1/permissions").respond(json={"permissions": [{"id": "p"}]})
        assert await w.list_permissions(USER, "f1") == [{"id": "p"}]
        route = respx.delete(f"{w.DRIVE_URL}/f1/permissions/p").respond(204)
        await w.unshare_file(USER, "f1", "p")
        assert route.called


class TestDocs:
    @respx.mock
    async def test_creating_with_text_appends_it(self):
        respx.post(w.DOCS_URL).respond(json={"documentId": "d1", "title": "t"})
        update = respx.post(f"{w.DOCS_URL}/d1:batchUpdate").respond(json={})
        assert await w.create_document(USER, title="t", text="hola") == {"id": "d1", "title": "t"}
        assert _json(update)["requests"][0]["insertText"]["text"] == "hola"

    @respx.mock
    async def test_creating_empty_writes_nothing(self):
        respx.post(w.DOCS_URL).respond(json={"documentId": "d1", "title": "t"})
        update = respx.post(f"{w.DOCS_URL}/d1:batchUpdate").respond(json={})
        await w.create_document(USER, title="t")
        assert not update.called

    @respx.mock
    async def test_replacing_says_how_many_changed(self):
        respx.post(f"{w.DOCS_URL}/d1:batchUpdate").mock(
            side_effect=[
                httpx.Response(
                    200, json={"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]}
                ),
                httpx.Response(200, json={}),
            ]
        )
        assert await w.replace_in_document(USER, "d1", find="a", replace="b") == 3
        assert await w.replace_in_document(USER, "d1", find="a", replace="b") == 0


class TestSheets:
    @respx.mock
    async def test_creating(self):
        respx.post(w.SHEETS_URL).respond(json={"spreadsheetId": "s1", "spreadsheetUrl": "u"})
        assert await w.create_spreadsheet(USER, title="t") == {"id": "s1", "link": "u"}

    @respx.mock
    async def test_a_range_with_a_space_stays_one_path_segment(self):
        route = respx.get(url__regex=r".*/s1/values/.*").respond(json={"values": [[1]]})
        assert await w.read_range(USER, "s1", "Hoja 1!A1:B2") == [[1]]
        assert "Hoja%201" in str(route.calls.last.request.url)

    @respx.mock
    async def test_writing_takes_formulas(self):
        route = respx.put(url__regex=r".*/s1/values/.*").respond(json={"updatedCells": 2})
        assert await w.write_range(USER, "s1", "A1", values=[["=1+1", 2]]) == 2
        assert route.calls.last.request.url.params["valueInputOption"] == "USER_ENTERED"

    @respx.mock
    async def test_appending(self):
        route = respx.post(url__regex=r".*/s1/values/A1:append").respond(json={})
        await w.append_rows(USER, "s1", "A1", values=[[1]])
        assert _json(route) == {"values": [[1]]}


class TestSlides:
    @respx.mock
    async def test_creating(self):
        respx.post(w.SLIDES_URL).respond(json={"presentationId": "p1", "title": "t"})
        assert await w.create_presentation(USER, title="t") == {"id": "p1", "title": "t"}

    @respx.mock
    async def test_a_slide_with_and_without_body(self):
        route = respx.post(f"{w.SLIDES_URL}/p1:batchUpdate").respond(json={})
        slide = await w.add_slide(USER, "p1", title="t", body="b")
        requests = _json(route)["requests"]
        assert requests[0]["createSlide"]["objectId"] == slide
        assert [r["insertText"]["text"] for r in requests[1:]] == ["t", "b"]
        await w.add_slide(USER, "p1", title="t")
        assert len(_json(route)["requests"]) == 2


class TestContacts:
    PERSON = {
        "resourceName": "people/c1",
        "etag": "e1",
        "names": [{"displayName": "Ana"}],
        "emailAddresses": [{"value": "a@b.c"}],
        "phoneNumbers": [{"value": "+34"}],
        "organizations": [{"name": "Nexo"}],
    }

    @respx.mock
    async def test_listing_everyone(self):
        respx.get(f"{w.PEOPLE_URL}/people/me/connections").respond(
            json={"connections": [self.PERSON, {"resourceName": "people/c2"}]}
        )
        ana, empty = await w.search_contacts(USER)
        assert ana == {
            "id": "people/c1",
            "name": "Ana",
            "emails": ["a@b.c"],
            "phones": ["+34"],
            "organization": "Nexo",
        }
        assert empty["name"] == ""

    @respx.mock
    async def test_searching(self):
        respx.get(f"{w.PEOPLE_URL}/people:searchContacts").respond(
            json={"results": [{"person": self.PERSON}]}
        )
        assert (await w.search_contacts(USER, "ana"))[0]["name"] == "Ana"

    @respx.mock
    async def test_creating(self):
        route = respx.post(f"{w.PEOPLE_URL}/people:createContact").respond(json=self.PERSON)
        await w.create_contact(USER, name="Ana", email="a@b.c", phone="+34", organization="Nexo")
        assert sorted(_json(route)) == [
            "emailAddresses",
            "names",
            "organizations",
            "phoneNumbers",
        ]

    @respx.mock
    async def test_updating_sends_the_etag_and_only_what_changed(self):
        respx.get(f"{w.PEOPLE_URL}/people/c1").respond(json=self.PERSON)
        route = respx.patch(f"{w.PEOPLE_URL}/people/c1:updateContact").respond(json=self.PERSON)
        await w.update_contact(USER, "people/c1", phone="+1")
        assert _json(route) == {"phoneNumbers": [{"value": "+1"}], "etag": "e1"}
        assert route.calls.last.request.url.params["updatePersonFields"] == "phoneNumbers"

    async def test_updating_nothing_is_refused(self):
        with pytest.raises(ValueError, match="Nothing"):
            await w.update_contact(USER, "people/c1")

    async def test_a_path_that_is_not_a_contact_is_refused(self):
        with pytest.raises(ValueError, match="contact"):
            await w.update_contact(USER, "otherContacts/c1", name="x")


class TestTasks:
    @respx.mock
    async def test_lists(self):
        respx.get(f"{w.TASKS_URL}/users/@me/lists").respond(
            json={"items": [{"id": "l1", "title": "Mi lista"}]}
        )
        assert await w.list_task_lists(USER) == [{"id": "l1", "title": "Mi lista"}]

    @respx.mock
    async def test_listing(self):
        route = respx.get(f"{w.TASKS_URL}/lists/@default/tasks").respond(
            json={"items": [{"id": "t1", "title": "x", "status": "completed"}]}
        )
        found = await w.list_tasks(USER, show_done=True)
        assert found[0]["done"] is True
        assert route.calls.last.request.url.params["showCompleted"] == "true"
        await w.list_tasks(USER)
        assert route.calls.last.request.url.params["showCompleted"] == "false"

    @respx.mock
    async def test_creating(self):
        route = respx.post(f"{w.TASKS_URL}/lists/@default/tasks").respond(json={"id": "t1"})
        await w.create_task(USER, title="x", notes="n", due="2026-11-03T00:00:00Z")
        assert _json(route) == {"title": "x", "notes": "n", "due": "2026-11-03T00:00:00Z"}
        await w.create_task(USER, title="x")
        assert _json(route) == {"title": "x"}

    @respx.mock
    async def test_updating(self):
        route = respx.patch(f"{w.TASKS_URL}/lists/@default/tasks/t1").respond(json={"id": "t1"})
        await w.update_task(USER, "t1", title="y", notes="n", due="d", done=True)
        assert _json(route) == {"title": "y", "notes": "n", "due": "d", "status": "completed"}
        await w.update_task(USER, "t1", done=False)
        assert _json(route) == {"status": "needsAction"}
        await w.update_task(USER, "t1")
        assert _json(route) == {}


class TestIdsInThePath:
    @pytest.mark.parametrize("bad", ["../../other", "a/b", "a?x=1", "a%2Fb", ""])
    async def test_what_is_not_a_google_id_never_goes_out(self, bad):
        with respx.mock(assert_all_called=False) as router:
            route = router.route()
            for call in (
                w.read_file(USER, bad),
                w.write_file(USER, bad, content="x"),
                w.update_file(USER, bad, name="x"),
                w.copy_file(USER, bad),
                w.set_trashed(USER, bad, True),
                w.list_permissions(USER, bad),
                w.share_file(USER, bad, email="a@b.c"),
                w.unshare_file(USER, "f1", bad),
                w.append_to_document(USER, bad, text="x"),
                w.read_range(USER, bad, "A1"),
                w.add_slide(USER, bad, title="x"),
                w.update_task(USER, bad, done=True),
                w.list_tasks(USER, bad),
                g.read_attachment(USER, bad, "a1"),
                g.trash_message(USER, bad),
                g.delete_filter(USER, bad),
            ):
                with pytest.raises(g.GoogleRequestError):
                    await call
            assert not route.called

    @pytest.mark.parametrize("bad", ["people/../x", "otherContacts/c1", "people/a/b"])
    async def test_a_contact_id_stays_a_contact_id(self, bad):
        with pytest.raises(ValueError, match="contact"):
            await w.update_contact(USER, bad, name="x")

    @respx.mock
    @pytest.mark.parametrize("bad", ["../../x", "a?b", "a#b", "a%2Fb", "a\\b", "", "x" * 257])
    async def test_a_range_cannot_add_to_the_path(self, bad):
        route = respx.get(url__regex=r".*/s1/values/.*").respond(json={"values": []})
        with pytest.raises(g.GoogleRequestError):
            await w.read_range(USER, "s1", bad)
        assert not route.called
