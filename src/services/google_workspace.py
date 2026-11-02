"""Drive, Docs, Sheets, Slides, Contacts and Tasks of the user's linked Google accounts.

The link and its tokens live in `google_service`. What is missing here is missing on
purpose: nothing deletes a file or a contact for good. Files go to the bin, where
Drive keeps them for 30 days, and a contact has no bin, so it has no delete at all.
Text from Google is returned as data, as in `google_service`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import quote

from src.services.google_service import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    GoogleRequestError,
    attachment_bytes,
    google_bytes,
    google_json,
    google_text,
    path_id,
)

DRIVE_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DOCS_URL = "https://docs.googleapis.com/v1/documents"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
SLIDES_URL = "https://slides.googleapis.com/v1/presentations"
PEOPLE_URL = "https://people.googleapis.com/v1"
TASKS_URL = "https://tasks.googleapis.com/tasks/v1"

FOLDER_MIME = "application/vnd.google-apps.folder"
#: What a Google file becomes when it is read: they have no bytes of their own.
EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
#: Stored files that can be read as text. Anything else comes back without any.
TEXT_MIMES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-python",
    "application/x-yaml",
)
#: Drive can turn these into a Google document, and that gives their text: the only
#: way to read a scan or a photo, with its text recognized by Google.
CONVERTIBLE_MIMES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-",
    "application/vnd.oasis.opendocument",
    "image/",
)
FILE_FIELDS = "id,name,mimeType,modifiedTime,size,parents,webViewLink,trashed"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations"
MAX_RESULTS = 50
SHARE_ROLES = ("reader", "commenter", "writer")
_CONTACT_ID = re.compile(r"people/[A-Za-z0-9_-]{1,256}")
#: A range is one path segment: A1 notation and a sheet's name, with nothing that ends it.
_CELL_RANGE = re.compile(r"[^/\?#%]{1,256}")


def _range_part(cell_range: str) -> str:
    if not _CELL_RANGE.fullmatch(cell_range):
        raise GoogleRequestError("Not a range")
    return quote(cell_range, safe="")


def _file(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "mime_type": item.get("mimeType"),
        "modified_at": item.get("modifiedTime"),
        "size": item.get("size"),
        "parents": item.get("parents", []),
        "link": item.get("webViewLink"),
        "trashed": item.get("trashed", False),
    }


# -- Drive ----------------------------------------------------------------------


async def search_files(
    user_id: str, query: str = "", max_results: int = 20, *, account: str | None = None
) -> list[dict[str, Any]]:
    """`query` is Drive's own syntax, such as `name contains 'factura'`."""
    listing = await google_json(
        user_id,
        "GET",
        DRIVE_URL,
        params={
            "q": f"({query}) and trashed = false" if query else "trashed = false",
            "pageSize": min(max_results, MAX_RESULTS),
            "fields": f"files({FILE_FIELDS})",
            "orderBy": "modifiedTime desc",
        },
        account=account,
    )
    return [_file(item) for item in listing.get("files", [])]


async def read_file(user_id: str, file_id: str, *, account: str | None = None) -> dict[str, Any]:
    """A file and its text. `text` is None for what has no text to read, such as a zip."""
    item = await google_json(
        user_id,
        "GET",
        f"{DRIVE_URL}/{path_id(file_id)}",
        params={"fields": FILE_FIELDS},
        account=account,
    )
    mime = str(item.get("mimeType", ""))
    text: str | None = None
    if mime in EXPORT_AS:
        text = await google_text(
            user_id,
            f"{DRIVE_URL}/{path_id(file_id)}/export",
            params={"mimeType": EXPORT_AS[mime]},
            account=account,
        )
    elif mime.startswith(TEXT_MIMES):
        text = await google_text(
            user_id, f"{DRIVE_URL}/{path_id(file_id)}", params={"alt": "media"}, account=account
        )
    elif mime.startswith(CONVERTIBLE_MIMES):
        text = await _text_by_conversion(user_id, file_id, account)
    return {**_file(item), "text": text}


async def _text_by_conversion(user_id: str, file_id: str, account: str | None) -> str:
    """Through a temporary Google document, removed afterwards.

    The one delete for good in this module, and only of the copy made here: to the bin
    it would fill the user's Drive with copies of what was read.
    """
    copy = await google_json(
        user_id,
        "POST",
        f"{DRIVE_URL}/{path_id(file_id)}/copy",
        params={"fields": "id"},
        body={"mimeType": "application/vnd.google-apps.document", "name": "Jarvis (temporal)"},
        account=account,
    )
    copy_id = str(copy.get("id"))
    try:
        return await google_text(
            user_id,
            f"{DRIVE_URL}/{copy_id}/export",
            params={"mimeType": "text/plain"},
            account=account,
        )
    finally:
        await google_json(user_id, "DELETE", f"{DRIVE_URL}/{copy_id}", account=account)


def _multipart(metadata: dict[str, Any], content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"kwami-{uuid.uuid4().hex}"
    head = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
    )
    return (
        head.encode() + content + f"\r\n--{boundary}--\r\n".encode(),
        f"multipart/related; boundary={boundary}",
    )


async def _upload(
    user_id: str,
    *,
    name: str,
    content: bytes,
    mime: str,
    parent_id: str | None,
    account: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if parent_id:
        metadata["parents"] = [parent_id]
    payload, content_type = _multipart(metadata, content, mime)
    created = await google_json(
        user_id,
        "POST",
        DRIVE_UPLOAD_URL,
        params={"uploadType": "multipart", "fields": FILE_FIELDS},
        content=payload,
        content_type=content_type,
        account=account,
    )
    return _file(created)


async def create_file(
    user_id: str,
    *,
    name: str,
    content: str = "",
    mime_type: str = "text/plain",
    parent_id: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    return await _upload(
        user_id,
        name=name,
        content=content.encode(),
        mime=f"{mime_type}; charset=UTF-8",
        parent_id=parent_id,
        account=account,
    )


async def save_attachment(
    user_id: str,
    message_id: str,
    attachment_id: str,
    *,
    filename: str,
    mime_type: str = "application/octet-stream",
    parent_id: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """An email's attachment into Drive, where `read_file` can give its text."""
    data = await attachment_bytes(user_id, message_id, attachment_id, account=account)
    return await _upload(
        user_id,
        name=filename,
        content=data,
        mime=mime_type,
        parent_id=parent_id,
        account=account,
    )


async def drive_attachments(
    user_id: str, file_ids: list[str], *, account: str | None = None
) -> list[Attachment]:
    """Drive files as email attachments. A Google document goes as a PDF."""
    attachments = []
    total = 0
    for file_id in file_ids:
        item = await google_json(
            user_id,
            "GET",
            f"{DRIVE_URL}/{path_id(file_id)}",
            params={"fields": "name,mimeType"},
            account=account,
        )
        name = str(item.get("name") or "archivo")
        mime = str(item.get("mimeType") or "application/octet-stream")
        if mime in EXPORT_AS:
            data = await google_bytes(
                user_id,
                f"{DRIVE_URL}/{path_id(file_id)}/export",
                params={"mimeType": "application/pdf"},
                account=account,
            )
            name, mime = f"{name}.pdf", "application/pdf"
        else:
            data = await google_bytes(
                user_id, f"{DRIVE_URL}/{path_id(file_id)}", params={"alt": "media"}, account=account
            )
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("The attachments are over Gmail's 25 MB")
        attachments.append(Attachment(name, mime, data))
    return attachments


async def write_file(
    user_id: str,
    file_id: str,
    *,
    content: str,
    mime_type: str = "text/plain",
    account: str | None = None,
) -> dict[str, Any]:
    """Replace a stored file's content. Drive keeps the earlier version in its history."""
    updated = await google_json(
        user_id,
        "PATCH",
        f"{DRIVE_UPLOAD_URL}/{path_id(file_id)}",
        params={"uploadType": "media", "fields": FILE_FIELDS},
        content=content.encode(),
        content_type=f"{mime_type}; charset=UTF-8",
        account=account,
    )
    return _file(updated)


async def create_folder(
    user_id: str, *, name: str, parent_id: str | None = None, account: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    created = await google_json(
        user_id, "POST", DRIVE_URL, params={"fields": FILE_FIELDS}, body=body, account=account
    )
    return _file(created)


async def update_file(
    user_id: str,
    file_id: str,
    *,
    name: str | None = None,
    add_parent: str | None = None,
    remove_parent: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Rename or move."""
    params = {"fields": FILE_FIELDS}
    if add_parent:
        params["addParents"] = add_parent
    if remove_parent:
        params["removeParents"] = remove_parent
    updated = await google_json(
        user_id,
        "PATCH",
        f"{DRIVE_URL}/{path_id(file_id)}",
        params=params,
        body={"name": name} if name else {},
        account=account,
    )
    return _file(updated)


async def copy_file(
    user_id: str, file_id: str, *, name: str | None = None, account: str | None = None
) -> dict[str, Any]:
    copied = await google_json(
        user_id,
        "POST",
        f"{DRIVE_URL}/{path_id(file_id)}/copy",
        params={"fields": FILE_FIELDS},
        body={"name": name} if name else {},
        account=account,
    )
    return _file(copied)


async def set_trashed(
    user_id: str, file_id: str, trashed: bool, *, account: str | None = None
) -> dict[str, Any]:
    updated = await google_json(
        user_id,
        "PATCH",
        f"{DRIVE_URL}/{path_id(file_id)}",
        params={"fields": FILE_FIELDS},
        body={"trashed": trashed},
        account=account,
    )
    return _file(updated)


async def list_permissions(
    user_id: str, file_id: str, *, account: str | None = None
) -> list[dict[str, Any]]:
    listing = await google_json(
        user_id,
        "GET",
        f"{DRIVE_URL}/{path_id(file_id)}/permissions",
        params={"fields": "permissions(id,type,role,emailAddress)"},
        account=account,
    )
    return list(listing.get("permissions", []))


async def share_file(
    user_id: str, file_id: str, *, email: str, role: str = "reader", account: str | None = None
) -> dict[str, Any]:
    """With one named person. There is no "anyone with the link", and no ownership."""
    if role not in SHARE_ROLES:
        raise ValueError(f"Role must be one of {', '.join(SHARE_ROLES)}")
    return await google_json(
        user_id,
        "POST",
        f"{DRIVE_URL}/{path_id(file_id)}/permissions",
        params={"fields": "id,type,role,emailAddress"},
        body={"type": "user", "role": role, "emailAddress": email},
        account=account,
    )


async def unshare_file(
    user_id: str, file_id: str, permission_id: str, *, account: str | None = None
) -> None:
    await google_json(
        user_id,
        "DELETE",
        f"{DRIVE_URL}/{path_id(file_id)}/permissions/{path_id(permission_id)}",
        account=account,
    )


# -- Docs -----------------------------------------------------------------------


async def create_document(
    user_id: str, *, title: str, text: str = "", account: str | None = None
) -> dict[str, Any]:
    created = await google_json(user_id, "POST", DOCS_URL, body={"title": title}, account=account)
    document_id = str(created.get("documentId"))
    if text:
        await append_to_document(user_id, document_id, text=text, account=account)
    return {"id": document_id, "title": created.get("title")}


async def append_to_document(
    user_id: str, document_id: str, *, text: str, account: str | None = None
) -> None:
    await google_json(
        user_id,
        "POST",
        f"{DOCS_URL}/{path_id(document_id)}:batchUpdate",
        body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]},
        account=account,
    )


async def replace_in_document(
    user_id: str, document_id: str, *, find: str, replace: str, account: str | None = None
) -> int:
    """Returns how many places changed."""
    result = await google_json(
        user_id,
        "POST",
        f"{DOCS_URL}/{path_id(document_id)}:batchUpdate",
        body={
            "requests": [
                {
                    "replaceAllText": {
                        "containsText": {"text": find, "matchCase": True},
                        "replaceText": replace,
                    }
                }
            ]
        },
        account=account,
    )
    replies = result.get("replies") or [{}]
    return int(replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0))


# -- Sheets ---------------------------------------------------------------------


async def create_spreadsheet(
    user_id: str, *, title: str, account: str | None = None
) -> dict[str, Any]:
    created = await google_json(
        user_id, "POST", SHEETS_URL, body={"properties": {"title": title}}, account=account
    )
    return {"id": created.get("spreadsheetId"), "link": created.get("spreadsheetUrl")}


async def read_range(
    user_id: str, spreadsheet_id: str, cell_range: str, *, account: str | None = None
) -> list[list[Any]]:
    result = await google_json(
        user_id,
        "GET",
        f"{SHEETS_URL}/{path_id(spreadsheet_id)}/values/{_range_part(cell_range)}",
        account=account,
    )
    return list(result.get("values", []))


async def write_range(
    user_id: str,
    spreadsheet_id: str,
    cell_range: str,
    *,
    values: list[list[Any]],
    account: str | None = None,
) -> int:
    """USER_ENTERED, so a formula works as if typed. Returns the cells changed."""
    result = await google_json(
        user_id,
        "PUT",
        f"{SHEETS_URL}/{path_id(spreadsheet_id)}/values/{_range_part(cell_range)}",
        params={"valueInputOption": "USER_ENTERED"},
        body={"values": values},
        account=account,
    )
    return int(result.get("updatedCells", 0))


async def append_rows(
    user_id: str,
    spreadsheet_id: str,
    cell_range: str,
    *,
    values: list[list[Any]],
    account: str | None = None,
) -> None:
    await google_json(
        user_id,
        "POST",
        f"{SHEETS_URL}/{path_id(spreadsheet_id)}/values/{_range_part(cell_range)}:append",
        params={"valueInputOption": "USER_ENTERED"},
        body={"values": values},
        account=account,
    )


# -- Slides ---------------------------------------------------------------------


async def create_presentation(
    user_id: str, *, title: str, account: str | None = None
) -> dict[str, Any]:
    created = await google_json(user_id, "POST", SLIDES_URL, body={"title": title}, account=account)
    return {"id": created.get("presentationId"), "title": created.get("title")}


async def add_slide(
    user_id: str,
    presentation_id: str,
    *,
    title: str,
    body: str = "",
    account: str | None = None,
) -> str:
    """A title-and-body slide at the end. Returns its id."""
    slide_id = f"s{uuid.uuid4().hex}"
    title_id = f"t{uuid.uuid4().hex}"
    body_id = f"b{uuid.uuid4().hex}"
    requests: list[dict[str, Any]] = [
        {
            "createSlide": {
                "objectId": slide_id,
                "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
                "placeholderIdMappings": [
                    {"layoutPlaceholder": {"type": "TITLE"}, "objectId": title_id},
                    {"layoutPlaceholder": {"type": "BODY"}, "objectId": body_id},
                ],
            }
        },
        {"insertText": {"objectId": title_id, "text": title}},
    ]
    if body:
        requests.append({"insertText": {"objectId": body_id, "text": body}})
    await google_json(
        user_id,
        "POST",
        f"{SLIDES_URL}/{path_id(presentation_id)}:batchUpdate",
        body={"requests": requests},
        account=account,
    )
    return slide_id


# -- Contacts -------------------------------------------------------------------


def _person(person: dict[str, Any]) -> dict[str, Any]:
    names = person.get("names") or [{}]
    return {
        "id": person.get("resourceName"),
        "name": names[0].get("displayName", ""),
        "emails": [item.get("value") for item in person.get("emailAddresses", [])],
        "phones": [item.get("value") for item in person.get("phoneNumbers", [])],
        "organization": (person.get("organizations") or [{}])[0].get("name", ""),
    }


def _person_body(
    name: str | None, email: str | None, phone: str | None, organization: str | None
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if name is not None:
        body["names"] = [{"givenName": name}]
    if email is not None:
        body["emailAddresses"] = [{"value": email}]
    if phone is not None:
        body["phoneNumbers"] = [{"value": phone}]
    if organization is not None:
        body["organizations"] = [{"name": organization}]
    return body


async def search_contacts(
    user_id: str, query: str = "", *, account: str | None = None
) -> list[dict[str, Any]]:
    """Everyone in the address book, or those matching `query`."""
    if query:
        found = await google_json(
            user_id,
            "GET",
            f"{PEOPLE_URL}/people:searchContacts",
            params={"query": query, "readMask": PERSON_FIELDS, "pageSize": 30},
            account=account,
        )
        return [_person(item.get("person", {})) for item in found.get("results", [])]
    listing = await google_json(
        user_id,
        "GET",
        f"{PEOPLE_URL}/people/me/connections",
        params={"personFields": PERSON_FIELDS, "pageSize": MAX_RESULTS},
        account=account,
    )
    return [_person(item) for item in listing.get("connections", [])]


async def create_contact(
    user_id: str,
    *,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    created = await google_json(
        user_id,
        "POST",
        f"{PEOPLE_URL}/people:createContact",
        params={"personFields": PERSON_FIELDS},
        body=_person_body(name, email, phone, organization),
        account=account,
    )
    return _person(created)


async def update_contact(
    user_id: str,
    resource_name: str,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    if not _CONTACT_ID.fullmatch(resource_name):
        raise ValueError("Not a contact id")
    body = _person_body(name, email, phone, organization)
    if not body:
        raise ValueError("Nothing to change")
    # Google refuses an update without the etag of the version being changed.
    current = await google_json(
        user_id,
        "GET",
        f"{PEOPLE_URL}/{resource_name}",
        params={"personFields": PERSON_FIELDS},
        account=account,
    )
    updated = await google_json(
        user_id,
        "PATCH",
        f"{PEOPLE_URL}/{resource_name}:updateContact",
        params={"updatePersonFields": ",".join(body), "personFields": PERSON_FIELDS},
        body={**body, "etag": current.get("etag")},
        account=account,
    )
    return _person(updated)


# -- Tasks ----------------------------------------------------------------------


def _task_list(task_list: str) -> str:
    return task_list if task_list == "@default" else path_id(task_list)


def _task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "title": task.get("title", ""),
        "notes": task.get("notes", ""),
        "due": task.get("due"),
        "done": task.get("status") == "completed",
    }


async def list_task_lists(user_id: str, *, account: str | None = None) -> list[dict[str, Any]]:
    listing = await google_json(user_id, "GET", f"{TASKS_URL}/users/@me/lists", account=account)
    return [
        {"id": item.get("id"), "title": item.get("title", "")} for item in listing.get("items", [])
    ]


async def list_tasks(
    user_id: str,
    task_list: str = "@default",
    *,
    show_done: bool = False,
    account: str | None = None,
) -> list[dict[str, Any]]:
    listing = await google_json(
        user_id,
        "GET",
        f"{TASKS_URL}/lists/{_task_list(task_list)}/tasks",
        params={"showCompleted": "true" if show_done else "false", "maxResults": 100},
        account=account,
    )
    return [_task(item) for item in listing.get("items", [])]


async def create_task(
    user_id: str,
    *,
    title: str,
    notes: str | None = None,
    due: str | None = None,
    task_list: str = "@default",
    account: str | None = None,
) -> dict[str, Any]:
    body = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = due
    created = await google_json(
        user_id,
        "POST",
        f"{TASKS_URL}/lists/{_task_list(task_list)}/tasks",
        body=body,
        account=account,
    )
    return _task(created)


async def update_task(
    user_id: str,
    task_id: str,
    *,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    done: bool | None = None,
    task_list: str = "@default",
    account: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due
    if done is not None:
        body["status"] = "completed" if done else "needsAction"
    updated = await google_json(
        user_id,
        "PATCH",
        f"{TASKS_URL}/lists/{_task_list(task_list)}/tasks/{path_id(task_id)}",
        body=body,
        account=account,
    )
    return _task(updated)
